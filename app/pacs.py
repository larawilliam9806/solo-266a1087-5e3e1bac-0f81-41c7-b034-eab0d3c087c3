"""研究 PACS 发送：从批次下载包筛选单帧 CT，经 DICOM C-STORE 逐件送达。

不变量：
- 只发送 Explicit VR Little Endian 的单帧 CT（CT Image Storage）；
- 收到 C-STORE 成功响应（0x0000）并把送达记录落库后，该实例才算“送达”；
- 同一目标已送达过同号且字节相同的实例一律跳过（跨批次生效）；
- 已送达记录的同号实例字节不同时，提示冲突并停止发送该实例（不影响其他实例）；
- 同一目标的同一实例不允许并发发送：认领（claim）在 IMMEDIATE 事务内完成，
  发送结果回写必须匹配认领令牌，崩溃遗留的认领过期后才能被接管；
- 重试只补送未确认或失败的实例；送达记录持久化在 SQLite，重启后依然有效；
- 网络 I/O 期间不持有数据库事务，连接失败/超时不阻塞下载与新批次发布。
"""
from __future__ import annotations

import hashlib
import io
import logging
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import pydicom
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian

from . import config
from .db import get_db, immediate_tx

LOGGER = logging.getLogger(__name__)

# 送达状态（deliveries.status）
SENDING = "sending"            # 已认领、发送进行中
DELIVERED = "delivered"        # 已收到成功响应并落库
REJECTED = "rejected"          # PACS 明确拒绝（含警告状态，按未送达处理）
UNCONFIRMED = "unconfirmed"    # 连接失败/超时/无响应，结果未知

C_STORE_SUCCESS = 0x0000


class PacsNotConfigured(Exception):
    """未配置研究 PACS（缺少 PACS_HOST）。"""


class BatchNotReady(Exception):
    """批次未就绪或下载包缺失，无可发送内容。"""


@dataclass
class PackageInstance:
    """下载包中的一个 DICOM 成员及其发送资格判定。"""

    member_name: str
    new_sop_uid: str
    sha256: str
    eligible: bool
    skip_reason: str = ""


@dataclass
class SendSummary:
    """一次发送运行的结果汇总（用于页面提示）。"""

    target_identity: str
    delivered: list[str] = field(default_factory=list)     # 本次新送达
    skipped: list[str] = field(default_factory=list)       # 已送达过，同字节跳过
    rejected: list[str] = field(default_factory=list)      # PACS 拒收
    unconfirmed: list[str] = field(default_factory=list)   # 未确认
    conflicts: list[str] = field(default_factory=list)     # 已送达记录字节冲突
    busy: list[str] = field(default_factory=list)          # 其他发送进行中
    not_eligible: int = 0                                  # 非单帧 CT 成员数

    @property
    def ok(self) -> bool:
        return not (self.rejected or self.unconfirmed or self.conflicts or self.busy)

    def message(self) -> str:
        parts = [f"目标 {self.target_identity}：新送达 {len(self.delivered)}"]
        if self.skipped:
            parts.append(f"已送达跳过 {len(self.skipped)}")
        if self.rejected:
            parts.append(f"拒收 {len(self.rejected)}")
        if self.unconfirmed:
            parts.append(f"未确认 {len(self.unconfirmed)}")
        if self.conflicts:
            shown = "、".join(self.conflicts[:3])
            extra = " 等" if len(self.conflicts) > 3 else ""
            parts.append(f"字节冲突已停止 {len(self.conflicts)}（{shown}{extra}）")
        if self.busy:
            parts.append(f"并发发送中跳过 {len(self.busy)}")
        if self.not_eligible:
            parts.append(f"非单帧 CT 不发送 {self.not_eligible}")
        return "；".join(parts)


# ---------------------------------------------------------------- 下载包筛选
def _classify_member(member_name: str, data: bytes) -> PackageInstance:
    digest = hashlib.sha256(data).hexdigest()
    try:
        ds = pydicom.dcmread(io.BytesIO(data), stop_before_pixels=True)
    except Exception:  # noqa: BLE001 - 包内成员损坏只影响该成员
        return PackageInstance(member_name, "", digest, False, "无法解析为 DICOM")
    sop_uid = str(ds.get("SOPInstanceUID") or "")
    ts = str(getattr(ds.file_meta, "TransferSyntaxUID", "") or "")
    if ts != str(ExplicitVRLittleEndian):
        return PackageInstance(
            member_name, sop_uid, digest, False,
            f"传输语法非 Explicit VR Little Endian（{ts or '缺失'}）",
        )
    sop_class = str(ds.get("SOPClassUID") or "")
    if sop_class != str(CTImageStorage):
        return PackageInstance(
            member_name, sop_uid, digest, False,
            f"非 CT 图像存储（SOP Class {sop_class or '缺失'}）",
        )
    frames = ds.get("NumberOfFrames")
    if frames is not None and str(frames) != "1":
        return PackageInstance(
            member_name, sop_uid, digest, False, f"多帧图像（{frames} 帧）"
        )
    if not sop_uid:
        return PackageInstance(member_name, "", digest, False, "缺少 SOPInstanceUID")
    return PackageInstance(member_name, sop_uid, digest, True)


def scan_batch_package(zip_path: Path) -> list[PackageInstance]:
    """扫描批次下载包，判定每个 DICOM 成员是否为可发送的单帧 CT。"""
    instances: list[PackageInstance] = []
    with zipfile.ZipFile(zip_path) as zf:
        for name in sorted(zf.namelist()):
            if not name.lower().endswith(".dcm"):
                continue
            instances.append(_classify_member(name, zf.read(name)))
    return instances


def _read_member(zip_path: Path, member_name: str) -> bytes:
    with zipfile.ZipFile(zip_path) as zf:
        return zf.read(member_name)


# ---------------------------------------------------------------- 认领与回写
def _claim_one(conn, target_id: str, inst: PackageInstance, batch_id: str,
               token: str, now: float, ttl: float) -> str:
    """在 IMMEDIATE 事务内认领一个实例。返回 claimed/skip/conflict/busy。"""
    row = conn.execute(
        "SELECT status, sha256, claimed_at FROM deliveries "
        "WHERE target=? AND new_sop_uid=?",
        (target_id, inst.new_sop_uid),
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO deliveries(target, new_sop_uid, sha256, status, batch_id, "
            "claim_token, claimed_at, updated_at) "
            "VALUES(?,?,?,?,?,?,?,datetime('now'))",
            (target_id, inst.new_sop_uid, inst.sha256, SENDING,
             batch_id, token, now),
        )
        return "claimed"
    if row["status"] == DELIVERED:
        # 同号同字节：跨批次也跳过；同号字节不同：冲突，停止发送该实例
        return "skip" if row["sha256"] == inst.sha256 else "conflict"
    if row["status"] == SENDING and now - row["claimed_at"] < ttl:
        return "busy"  # 另一发送正在进行，同一实例不得并发发送
    # rejected / unconfirmed / 过期 sending：接管认领，补送
    conn.execute(
        "UPDATE deliveries SET status=?, sha256=?, batch_id=?, claim_token=?, "
        "claimed_at=?, dimse_status=NULL, message='', updated_at=datetime('now') "
        "WHERE target=? AND new_sop_uid=?",
        (SENDING, inst.sha256, batch_id, token, now,
         target_id, inst.new_sop_uid),
    )
    return "claimed"


def _owns_claim(target_id: str, new_sop_uid: str, token: str) -> bool:
    """发送前确认认领仍归本次运行所有（防止过期后被其他运行接管）。"""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT 1 FROM deliveries WHERE target=? AND new_sop_uid=? "
            "AND status=? AND claim_token=?",
            (target_id, new_sop_uid, SENDING, token),
        ).fetchone()
        return row is not None
    finally:
        conn.close()


def _record_result(target_id: str, new_sop_uid: str, token: str, status: str,
                   dimse_status: int | None, message: str) -> None:
    """回写发送结果。只有持有认领令牌的运行才能落库。

    “送达”状态的落库即送达判定的最后一步：收到成功响应后记录保存成功，
    该实例才算送达；保存失败则维持 sending，等待过期后按未确认补送。
    """
    conn = get_db()
    try:
        with immediate_tx(conn):
            cur = conn.execute(
                "UPDATE deliveries SET status=?, dimse_status=?, message=?, "
                "updated_at=datetime('now') "
                "WHERE target=? AND new_sop_uid=? AND claim_token=?",
                (status, dimse_status, message, target_id, new_sop_uid, token),
            )
            if cur.rowcount == 0:
                LOGGER.warning("实例 %s 的认领已被接管，结果未落库（%s）",
                               new_sop_uid, status)
    finally:
        conn.close()


def _mark_unconfirmed(target_id: str, instances: list[PackageInstance],
                      token: str, message: str) -> None:
    """关联失败等场景下，把本次认领的实例统一标记为未确认。"""
    conn = get_db()
    try:
        with immediate_tx(conn):
            for inst in instances:
                conn.execute(
                    "UPDATE deliveries SET status=?, message=?, "
                    "updated_at=datetime('now') "
                    "WHERE target=? AND new_sop_uid=? AND claim_token=?",
                    (UNCONFIRMED, message, target_id, inst.new_sop_uid, token),
                )
    finally:
        conn.close()


# ---------------------------------------------------------------- 网络发送
def _associate(target: config.PacsTarget):
    """建立到研究 PACS 的 DICOM 关联（仅请求 CT/EVLE 上下文）。"""
    from pynetdicom import AE  # 延迟导入，纯页面访问不依赖网络库

    ae = AE(ae_title=target.calling_ae_title)
    connect_timeout = config.get_pacs_connect_timeout()
    ae.connection_timeout = connect_timeout   # TCP 连接超时
    ae.acse_timeout = connect_timeout         # 关联协商超时
    ae.network_timeout = connect_timeout      # 网络空闲超时
    ae.dimse_timeout = config.get_pacs_dimse_timeout()  # 等待 C-STORE 响应
    ae.add_requested_context(CTImageStorage, ExplicitVRLittleEndian)
    return ae.associate(target.host, target.port, ae_title=target.ae_title)


def send_batch(batch_id: str) -> SendSummary:
    """把批次下载包中的单帧 CT 逐件 C-STORE 到研究 PACS。

    任何单实例失败不影响其他实例；关联失败时本次认领的实例统一记未确认。
    """
    try:
        target = config.get_pacs_target()
    except ValueError as exc:
        raise PacsNotConfigured(f"研究 PACS 配置非法: {exc}") from exc
    if target is None:
        raise PacsNotConfigured(
            "未配置研究 PACS（请设置 PACS_HOST / PACS_PORT / PACS_AE_TITLE）"
        )
    from . import services  # 避免循环导入

    batch = services.get_batch(batch_id)
    if batch is None:
        raise BatchNotReady(f"批次不存在: {batch_id}")
    if batch["status"] != "ready" or not batch["zip_path"]:
        raise BatchNotReady("批次未处理成功，无下载包可发送")
    zip_path = Path(batch["zip_path"])
    if not zip_path.exists():
        raise BatchNotReady("批次下载包缺失，无法发送")

    instances = scan_batch_package(zip_path)
    eligible = [i for i in instances if i.eligible]
    summary = SendSummary(
        target_identity=target.identity,
        not_eligible=len(instances) - len(eligible),
    )

    # 1) 认领阶段：单个 IMMEDIATE 事务内完成全部判定，保证跨进程互斥
    token = uuid.uuid4().hex
    now = time.time()
    ttl = config.get_send_claim_ttl()
    to_send: list[PackageInstance] = []
    conn = get_db()
    try:
        with immediate_tx(conn):
            for inst in eligible:
                decision = _claim_one(conn, target.identity, inst,
                                      batch_id, token, now, ttl)
                if decision == "claimed":
                    to_send.append(inst)
                elif decision == "skip":
                    summary.skipped.append(inst.new_sop_uid)
                elif decision == "conflict":
                    summary.conflicts.append(inst.new_sop_uid)
                else:
                    summary.busy.append(inst.new_sop_uid)
    finally:
        conn.close()

    if not to_send:
        return summary

    # 2) 建立关联（网络 I/O 不持有任何数据库事务，失败不影响下载/发布）
    try:
        assoc = _associate(target)
    except Exception as exc:  # noqa: BLE001 - 连接异常统一记未确认
        LOGGER.warning("连接研究 PACS %s 失败: %s", target.identity, exc)
        _mark_unconfirmed(target.identity, to_send, token, f"连接失败: {exc}")
        summary.unconfirmed.extend(i.new_sop_uid for i in to_send)
        return summary
    if not assoc.is_established:
        _mark_unconfirmed(target.identity, to_send, token,
                          f"关联被研究 PACS 拒绝（{target.identity}）")
        summary.unconfirmed.extend(i.new_sop_uid for i in to_send)
        try:
            assoc.release()
        except Exception:  # noqa: BLE001
            pass
        return summary

    # 3) 逐件发送：每件独立判定与落库，互不影响
    try:
        for inst in to_send:
            uid = inst.new_sop_uid
            if not _owns_claim(target.identity, uid, token):
                summary.busy.append(uid)
                continue
            try:
                ds = pydicom.dcmread(io.BytesIO(_read_member(zip_path,
                                                             inst.member_name)))
                status = assoc.send_c_store(ds)
            except Exception as exc:  # noqa: BLE001 - 单件异常不中断整批
                LOGGER.warning("C-STORE %s 异常: %s", uid, exc)
                _record_result(target.identity, uid, token, UNCONFIRMED,
                               None, f"发送异常: {exc}")
                summary.unconfirmed.append(uid)
                continue
            # 超时/对端中断时 pynetdicom 返回 None 或不含 Status 的空 Dataset
            code = getattr(status, "Status", None) if status is not None else None
            if code is None:
                _record_result(target.identity, uid, token, UNCONFIRMED,
                               None, "DIMSE 超时或对端中断，结果未知")
                summary.unconfirmed.append(uid)
            elif int(code) == C_STORE_SUCCESS:
                _record_result(target.identity, uid, token, DELIVERED,
                               int(code), "C-STORE 成功")
                summary.delivered.append(uid)
            else:
                code = int(code)
                _record_result(target.identity, uid, token, REJECTED,
                               code, f"PACS 拒绝，DIMSE 状态 0x{code:04X}")
                summary.rejected.append(uid)
    finally:
        try:
            assoc.release()
        except Exception:  # noqa: BLE001
            pass
    return summary


# ---------------------------------------------------------------- 页面视图
# 视图状态 -> 展示文案（pending 表示从未发送，同样属于未确认）
STATE_LABELS = {
    "delivered": "送达",
    "rejected": "拒收",
    "unconfirmed": "未确认",
    "sending": "发送中",
    "conflict": "冲突（已送达记录字节不一致，停止发送）",
    "pending": "未确认（尚未发送）",
}


@dataclass
class DeliveryViewItem:
    new_sop_uid: str
    state: str
    label: str
    dimse_status: int | None
    message: str
    updated_at: str


def _view_item(inst: PackageInstance, row, now: float, ttl: float) -> DeliveryViewItem:
    if row is None:
        state = "pending"
        return DeliveryViewItem(inst.new_sop_uid, state, STATE_LABELS[state],
                                None, "", "")
    status = row["status"]
    if status == DELIVERED and row["sha256"] != inst.sha256:
        status = "conflict"
    elif status == SENDING and now - row["claimed_at"] >= ttl:
        status = "unconfirmed"  # 认领过期，发送方已失联
    return DeliveryViewItem(
        inst.new_sop_uid, status, STATE_LABELS[status],
        row["dimse_status"], row["message"], row["updated_at"],
    )


def batch_delivery_view(batch) -> dict | None:
    """批次详情页的发送区视图；批次未就绪时返回 None（不显示发送区）。"""
    if batch["status"] != "ready" or not batch["zip_path"]:
        return None
    zip_path = Path(batch["zip_path"])
    if not zip_path.exists():
        return None
    target = None
    config_error = ""
    try:
        target = config.get_pacs_target()
    except ValueError as exc:
        config_error = str(exc)  # 配置非法不拖垮批次详情页
    instances = scan_batch_package(zip_path)
    eligible = [i for i in instances if i.eligible]
    skipped = [(i.member_name, i.skip_reason) for i in instances if not i.eligible]

    deliveries: dict[str, object] = {}
    if target is not None and eligible:
        conn = get_db()
        try:
            placeholders = ",".join("?" * len(eligible))
            rows = conn.execute(
                f"SELECT * FROM deliveries WHERE target=? "
                f"AND new_sop_uid IN ({placeholders})",
                (target.identity, *[i.new_sop_uid for i in eligible]),
            ).fetchall()
            deliveries = {r["new_sop_uid"]: r for r in rows}
        finally:
            conn.close()

    now = time.time()
    ttl = config.get_send_claim_ttl()
    items = [_view_item(inst, deliveries.get(inst.new_sop_uid), now, ttl)
             for inst in eligible]
    return {"target": target, "rows": items, "skipped": skipped,
            "config_error": config_error}


# ---------------------------------------------------------------- 启动恢复
def recover_stale_send_claims() -> int:
    """把过期 sending 认领标记为未确认（崩溃/重启前未完成的发送不算送达）。

    仍在 TTL 内的新鲜认领可能属于其他存活 worker，保持不动；
    已送达/已拒收记录原样保留，重启后重试只补送未确认或失败的实例。
    """
    cutoff = time.time() - config.get_send_claim_ttl()
    conn = get_db()
    try:
        with immediate_tx(conn):
            cur = conn.execute(
                "UPDATE deliveries SET status=?, "
                "message='发送中断（服务重启或认领过期）', updated_at=datetime('now') "
                "WHERE status=? AND claimed_at < ?",
                (UNCONFIRMED, SENDING, cutoff),
            )
            return cur.rowcount
    finally:
        conn.close()
