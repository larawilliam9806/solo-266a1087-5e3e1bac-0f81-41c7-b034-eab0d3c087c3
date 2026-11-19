"""研究 PACS 发送（C-STORE）测试：送达/拒收/未确认、跳过、冲突、并发与恢复。"""
from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import pydicom
import pytest
from werkzeug.datastructures import MultiDict

from samples import generate_samples as gen


# ---------------------------------------------------------------- 工具
class ScpServer:
    """本机 Storage SCP：记录收到的实例，可配置响应状态与处理延时。"""

    def __init__(self, response_status: int = 0x0000, store_delay: float = 0.0):
        self.response_status = response_status
        self.store_delay = store_delay
        self.received: list[pydicom.Dataset] = []
        self.store_calls = 0
        self.port: int | None = None
        self._server = None

    def start(self) -> "ScpServer":
        from pynetdicom import (
            ALL_TRANSFER_SYNTAXES,
            AE,
            AllStoragePresentationContexts,
            evt,
        )
        from pynetdicom.sop_class import Verification

        ae = AE(ae_title="RESEARCH_PACS")
        for ctx in AllStoragePresentationContexts:
            ae.add_supported_context(ctx.abstract_syntax, ALL_TRANSFER_SYNTAXES)
        ae.add_supported_context(Verification, ALL_TRANSFER_SYNTAXES)

        def handle_store(event):
            self.store_calls += 1
            if self.store_delay:
                time.sleep(self.store_delay)
            ds = event.dataset
            ds.file_meta = event.file_meta
            self.received.append(ds)
            return self.response_status

        self._server = ae.start_server(
            ("127.0.0.1", 0), block=False,
            evt_handlers=[(evt.EVT_C_STORE, handle_store)],
        )
        self.port = self._server.socket.getsockname()[1]
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server = None

    @property
    def received_uids(self) -> list[str]:
        return [str(ds.SOPInstanceUID) for ds in self.received]


@pytest.fixture()
def scp():
    server = ScpServer().start()
    yield server
    server.stop()


@pytest.fixture()
def pacs_env(monkeypatch, scp):
    """把应用指向本机 SCP。"""
    monkeypatch.setenv("PACS_HOST", "127.0.0.1")
    monkeypatch.setenv("PACS_PORT", str(scp.port))
    monkeypatch.setenv("PACS_AE_TITLE", "RESEARCH_PACS")
    monkeypatch.setenv("PACS_CALLING_AE_TITLE", "DICOM_BATCH")
    monkeypatch.setenv("PACS_CONNECT_TIMEOUT", "5")
    monkeypatch.setenv("PACS_DIMSE_TIMEOUT", "5")
    return scp


def _upload_batch(client, paths) -> str:
    data = MultiDict()
    handles = []
    for p in paths:
        fh = open(p, "rb")
        handles.append(fh)
        data.add("dicom_files", (fh, Path(p).name, "application/dicom"))
    resp = client.post("/upload", data=data, content_type="multipart/form-data")
    for fh in handles:
        fh.close()
    assert resp.status_code == 302
    return resp.headers["Location"].rstrip("/").split("/")[-1]


def _send(client, batch_id: str):
    return client.post(f"/batches/{batch_id}/send", follow_redirects=True)


def _new_sop_uid(original_uid: str) -> str:
    from app.identifiers import derive_uid
    return derive_uid("sop", original_uid)


def _deliveries():
    from app.db import get_db
    conn = get_db()
    try:
        return conn.execute(
            "SELECT * FROM deliveries ORDER BY new_sop_uid"
        ).fetchall()
    finally:
        conn.close()


# ---------------------------------------------------------------- 基本送达
def test_send_delivers_single_frame_cts(client, sample_files, pacs_env):
    paths = sorted(sample_files.glob("*.dcm"))
    batch_id = _upload_batch(client, paths)

    resp = _send(client, batch_id)
    assert resp.status_code == 200
    assert "新送达 2".encode() in resp.data

    # 只有两张单帧 CT 被发送；RT/SR 不发送
    assert pacs_env.store_calls == 2
    expected = {_new_sop_uid(gen.SOP_CT_1), _new_sop_uid(gen.SOP_CT_2)}
    assert set(pacs_env.received_uids) == expected

    # 收到的是匿名化副本：匿名 SOP UID、派生 PatientID，原始标识不出现
    for ds in pacs_env.received:
        assert str(ds.SOPInstanceUID) in expected
        assert str(ds.PatientID).startswith("P")
        assert str(ds.PatientID) != gen.PATIENT_ID
        assert str(ds.StudyInstanceUID) != gen.STUDY_UID

    # 送达记录已落库（收到成功响应并保存记录后才算送达）
    rows = _deliveries()
    assert len(rows) == 2
    for row in rows:
        assert row["status"] == "delivered"
        assert row["dimse_status"] == 0
        assert row["target"].endswith(f":{pacs_env.port}")

    # 页面按匿名 SOPInstanceUID 显示送达状态
    page = client.get(f"/batches/{batch_id}")
    for uid in expected:
        assert uid.encode() in page.data
    assert "送达".encode() in page.data
    # 非 CT 成员列为不发送
    assert "非 CT 图像存储".encode() in page.data


# ---------------------------------------------------------------- 跨批次同字节跳过
def test_identical_bytes_skipped_across_batches(client, sample_files, pacs_env):
    paths = sorted(sample_files.glob("*.dcm"))
    b1 = _upload_batch(client, paths)
    _send(client, b1)
    assert pacs_env.store_calls == 2

    # 另一批次包含同一实例（同号同字节）：整件跳过，不再发送
    b2 = _upload_batch(client, paths)
    resp = _send(client, b2)
    assert pacs_env.store_calls == 2
    assert "已送达跳过 2".encode() in resp.data
    page = client.get(f"/batches/{b2}")
    assert "送达".encode() in page.data


# ---------------------------------------------------------------- 已送达同号字节冲突
def test_delivered_byte_conflict_stops_instance(client, sample_files, pacs_env):
    paths = sorted(sample_files.glob("*.dcm"))
    batch_id = _upload_batch(client, paths)
    _send(client, batch_id)
    assert pacs_env.store_calls == 2

    # 模拟“同一目标已送达过同号但字节不同的影像”
    conflict_uid = _new_sop_uid(gen.SOP_CT_1)
    from app.db import get_db, immediate_tx
    conn = get_db()
    try:
        with immediate_tx(conn):
            conn.execute(
                "UPDATE deliveries SET sha256='deadbeef' WHERE new_sop_uid=?",
                (conflict_uid,),
            )
    finally:
        conn.close()

    resp = _send(client, batch_id)
    # 冲突实例停止发送；另一实例同字节正常跳过；没有任何新发送
    assert pacs_env.store_calls == 2
    assert "字节冲突已停止 1".encode() in resp.data
    assert conflict_uid.encode() in resp.data
    page = client.get(f"/batches/{batch_id}")
    assert "冲突".encode() in page.data


# ---------------------------------------------------------------- 拒收与重试
def test_rejection_recorded_and_retried(client, sample_files, pacs_env):
    pacs_env.response_status = 0xA700  # OutOfResources
    paths = sorted(sample_files.glob("*.dcm"))
    batch_id = _upload_batch(client, paths)

    resp = _send(client, batch_id)
    assert "拒收 2".encode() in resp.data
    rows = _deliveries()
    assert {r["status"] for r in rows} == {"rejected"}
    assert {r["dimse_status"] for r in rows} == {0xA700}
    page = client.get(f"/batches/{batch_id}")
    assert "拒收".encode() in page.data
    assert b"0xA700" in page.data

    # PACS 恢复后重试：拒收实例被补送
    pacs_env.response_status = 0x0000
    resp = _send(client, batch_id)
    assert "新送达 2".encode() in resp.data
    assert pacs_env.store_calls == 4
    assert {r["status"] for r in _deliveries()} == {"delivered"}


# ---------------------------------------------------------------- 连接失败不阻塞下载与发布
def test_connection_failure_unconfirmed_and_services_unaffected(
    client, sample_files, monkeypatch
):
    paths = sorted(sample_files.glob("*.dcm"))
    batch_id = _upload_batch(client, paths)

    # 指向一个会接受 TCP 但永不回应的黑洞端点，发送期间连接挂起
    blocker = socket.socket()
    blocker.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(4)
    port = blocker.getsockname()[1]
    stop = threading.Event()

    def blackhole():
        blocker.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = blocker.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=lambda c=conn: time.sleep(6), daemon=True).start()

    thread = threading.Thread(target=blackhole, daemon=True)
    thread.start()
    monkeypatch.setenv("PACS_HOST", "127.0.0.1")
    monkeypatch.setenv("PACS_PORT", str(port))
    monkeypatch.setenv("PACS_CONNECT_TIMEOUT", "2")
    monkeypatch.setenv("PACS_DIMSE_TIMEOUT", "2")
    try:
        result: dict = {}

        def do_send():
            result["resp"] = _send(client, batch_id)

        send_thread = threading.Thread(target=do_send)
        send_thread.start()
        time.sleep(0.5)  # 发送正处于连接失败/超时窗口
        # 原批次仍可下载
        assert client.get(f"/batches/{batch_id}/download").status_code == 200
        # 新批次仍可发布
        other = _upload_batch(client, [sample_files / "ct_001.dcm"])
        assert client.get(f"/batches/{other}/download").status_code == 200
        send_thread.join(timeout=30)
        resp = result["resp"]
        assert "未确认 2".encode() in resp.data
    finally:
        stop.set()
        blocker.close()
        thread.join(timeout=5)

    assert {r["status"] for r in _deliveries()} == {"unconfirmed"}
    page = client.get(f"/batches/{batch_id}")
    assert "未确认".encode() in page.data


# ---------------------------------------------------------------- DIMSE 超时
def test_dimse_timeout_marks_unconfirmed(client, sample_files, monkeypatch):
    slow_scp = ScpServer(store_delay=3.0).start()
    monkeypatch.setenv("PACS_HOST", "127.0.0.1")
    monkeypatch.setenv("PACS_PORT", str(slow_scp.port))
    monkeypatch.setenv("PACS_CONNECT_TIMEOUT", "2")
    monkeypatch.setenv("PACS_DIMSE_TIMEOUT", "1")
    try:
        batch_id = _upload_batch(client, [sample_files / "ct_001.dcm"])
        resp = _send(client, batch_id)
        assert "未确认 1".encode() in resp.data
        assert {r["status"] for r in _deliveries()} == {"unconfirmed"}
    finally:
        slow_scp.stop()


# ---------------------------------------------------------------- 重启后按送达记录判断
def test_delivered_records_survive_restart(app, client, sample_files, pacs_env):
    paths = sorted(sample_files.glob("*.dcm"))
    batch_id = _upload_batch(client, paths)
    _send(client, batch_id)
    assert pacs_env.store_calls == 2

    # 模拟服务重启：同一数据目录重新创建应用
    from app.web import create_app
    app2 = create_app()
    client2 = app2.test_client()
    resp = client2.post(f"/batches/{batch_id}/send", follow_redirects=True)
    assert "已送达跳过 2".encode() in resp.data
    assert pacs_env.store_calls == 2  # 没有重复发送


# ---------------------------------------------------------------- 过期认领恢复
def test_stale_claim_recovered_and_retried(client, sample_files, pacs_env):
    batch_id = _upload_batch(client, [sample_files / "ct_001.dcm"])
    uid = _new_sop_uid(gen.SOP_CT_1)

    # 模拟崩溃遗留的过期 sending 认领
    from app import config
    from app.db import get_db, immediate_tx
    target = config.get_pacs_target().identity
    conn = get_db()
    try:
        with immediate_tx(conn):
            conn.execute(
                "INSERT INTO deliveries(target, new_sop_uid, sha256, status, "
                "claim_token, claimed_at) VALUES(?,?,?,?,?,?)",
                (target, uid, "stale", "sending", "dead-token", time.time() - 10000),
            )
    finally:
        conn.close()

    from app.pacs import recover_stale_send_claims
    assert recover_stale_send_claims() == 1
    assert _deliveries()[0]["status"] == "unconfirmed"

    # 重试补送该实例并成功送达
    resp = _send(client, batch_id)
    assert "新送达 1".encode() in resp.data
    assert pacs_env.store_calls == 1
    assert _deliveries()[0]["status"] == "delivered"


# ---------------------------------------------------------------- 同一实例并发发送互斥
def test_concurrent_sends_same_instance_excluded(app, sample_files, monkeypatch):
    slow_scp = ScpServer(store_delay=0.6).start()
    monkeypatch.setenv("PACS_HOST", "127.0.0.1")
    monkeypatch.setenv("PACS_PORT", str(slow_scp.port))
    monkeypatch.setenv("PACS_CONNECT_TIMEOUT", "5")
    monkeypatch.setenv("PACS_DIMSE_TIMEOUT", "5")
    try:
        client = app.test_client()
        paths = sorted(sample_files.glob("*.dcm"))
        batch_id = _upload_batch(client, paths)

        barrier = threading.Barrier(2)
        responses: list = []

        def worker():
            c = app.test_client()
            barrier.wait()
            responses.append(c.post(f"/batches/{batch_id}/send",
                                    follow_redirects=True))

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        # 同一目标的同一实例绝不并发发送：每件只发一次
        assert slow_scp.store_calls == 2
        assert {r["status"] for r in _deliveries()} == {"delivered"}
        bodies = b"\n".join(r.data for r in responses)
        assert "并发发送中跳过".encode() in bodies or "已送达跳过".encode() in bodies
    finally:
        slow_scp.stop()


# ---------------------------------------------------------------- 发送资格筛选
def test_only_evle_single_frame_ct_is_sent(client, sample_files, pacs_env, tmp_path):
    ct = pydicom.dcmread(str(sample_files / "ct_001.dcm"))

    # Implicit VR Little Endian 的 CT：可上传（未压缩），但不属于 EVLE，不发送
    ivle = pydicom.dcmread(str(sample_files / "ct_001.dcm"))
    ivle.SOPInstanceUID = gen.SOP_CT_1 + ".101"
    ivle.file_meta.MediaStorageSOPInstanceUID = ivle.SOPInstanceUID
    ivle.file_meta.TransferSyntaxUID = pydicom.uid.ImplicitVRLittleEndian
    ivle.is_implicit_VR = True
    ivle_path = tmp_path / "ct_ivle.dcm"
    ivle.save_as(str(ivle_path), write_like_original=False)

    # 多帧 CT（EVLE）：不发送
    multi = pydicom.dcmread(str(sample_files / "ct_001.dcm"))
    multi.SOPInstanceUID = gen.SOP_CT_1 + ".102"
    multi.file_meta.MediaStorageSOPInstanceUID = multi.SOPInstanceUID
    multi.NumberOfFrames = 3
    multi.PixelData = ct.PixelData * 3
    multi_path = tmp_path / "ct_multi.dcm"
    multi.save_as(str(multi_path), write_like_original=False)

    batch_id = _upload_batch(
        client, [sample_files / "ct_001.dcm", ivle_path, multi_path])
    resp = _send(client, batch_id)
    assert "新送达 1".encode() in resp.data
    assert pacs_env.store_calls == 1
    assert pacs_env.received_uids == [_new_sop_uid(gen.SOP_CT_1)]

    page = client.get(f"/batches/{batch_id}")
    assert "传输语法非 Explicit VR Little Endian".encode() in page.data
    assert "多帧图像".encode() in page.data


# ---------------------------------------------------------------- 前置条件
def test_send_requires_ready_batch(client, sample_files, pacs_env, tmp_path):
    junk = tmp_path / "junk.dcm"
    junk.write_bytes(b"not dicom" * 10)
    batch_id = _upload_batch(client, [junk])
    resp = _send(client, batch_id)
    assert "无下载包可发送".encode() in resp.data
    assert pacs_env.store_calls == 0


def test_send_requires_pacs_configured(client, sample_files, monkeypatch):
    monkeypatch.delenv("PACS_HOST", raising=False)
    batch_id = _upload_batch(client, [sample_files / "ct_001.dcm"])
    resp = _send(client, batch_id)
    assert "未配置研究 PACS".encode() in resp.data
    page = client.get(f"/batches/{batch_id}")
    assert "未配置研究 PACS".encode() in page.data


def test_invalid_pacs_config_does_not_break_page(client, sample_files, monkeypatch):
    monkeypatch.setenv("PACS_HOST", "127.0.0.1")
    monkeypatch.setenv("PACS_PORT", "not-a-port")
    batch_id = _upload_batch(client, [sample_files / "ct_001.dcm"])
    page = client.get(f"/batches/{batch_id}")
    assert page.status_code == 200
    assert "配置非法".encode() in page.data
    resp = _send(client, batch_id)
    assert "配置非法".encode() in resp.data


def test_send_unknown_batch_404(client, pacs_env):
    resp = client.post("/batches/nonexistent/send")
    assert resp.status_code == 404
