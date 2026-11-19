"""本机研究 PACS 模拟端（DICOM Storage SCP）。

用于本地联调与 docker compose：接受任意 Storage SOP Class 的 C-STORE，
按 SOPInstanceUID 落盘到存储目录；同时支持 C-ECHO 便于连通性检查。

环境变量：
  SCP_AE_TITLE         接收端 AE Title（默认 RESEARCH_PACS）
  SCP_HOST             监听地址（默认 0.0.0.0）
  SCP_PORT             监听端口（默认 11112）
  SCP_STORAGE_DIR      接收文件存放目录（默认 ./scp_incoming）
  SCP_RESPONSE_STATUS  C-STORE 响应状态码（默认 0x0000；设为 0xA700 可模拟拒收）

运行：python -m app.scp_server
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from pynetdicom import (
    ALL_TRANSFER_SYNTAXES,
    AE,
    AllStoragePresentationContexts,
    evt,
)
from pynetdicom.sop_class import Verification

LOGGER = logging.getLogger("scp_server")


def _handle_store(storage_dir: Path, response_status: int):
    def handler(event):
        try:
            ds = event.dataset
            ds.file_meta = event.file_meta
            sop_uid = str(ds.SOPInstanceUID)
            # SOPInstanceUID 仅含数字与点，天然文件名安全
            final_path = storage_dir / f"{sop_uid}.dcm"
            tmp_path = storage_dir / f".{sop_uid}.dcm.tmp"
            ds.save_as(str(tmp_path), write_like_original=False)
            os.replace(tmp_path, final_path)
            LOGGER.info("C-STORE 接收 %s -> %s", sop_uid, final_path)
        except Exception:  # noqa: BLE001
            LOGGER.exception("接收落盘失败")
            return 0xA700  # OutOfResources
        return response_status

    return handler


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    ae_title = os.environ.get("SCP_AE_TITLE", "RESEARCH_PACS").strip() or "RESEARCH_PACS"
    host = os.environ.get("SCP_HOST", "0.0.0.0")
    port = int(os.environ.get("SCP_PORT", "11112"))
    storage_dir = Path(os.environ.get("SCP_STORAGE_DIR", "./scp_incoming"))
    storage_dir.mkdir(parents=True, exist_ok=True)
    response_status = int(os.environ.get("SCP_RESPONSE_STATUS", "0x0000"), 16)

    ae = AE(ae_title=ae_title)
    # 接收端尽量通用：支持全部 Storage SOP Class 与所有传输语法
    for context in AllStoragePresentationContexts:
        ae.add_supported_context(context.abstract_syntax, ALL_TRANSFER_SYNTAXES)
    ae.add_supported_context(Verification, ALL_TRANSFER_SYNTAXES)

    handlers = [(evt.EVT_C_STORE, _handle_store(storage_dir, response_status))]
    LOGGER.info(
        "研究 PACS 模拟端启动：AE=%s，监听 %s:%s，存储目录 %s，C-STORE 响应 0x%04X",
        ae_title, host, port, storage_dir, response_status,
    )
    ae.start_server((host, port), block=True, evt_handlers=handlers)


if __name__ == "__main__":
    main()
