"""运行配置。所有路径与密钥前缀均可通过环境变量覆盖。

路径类与 PACS 目标配置在访问时读取环境变量，便于测试在不重载模块的情况下
隔离数据目录与接收端地址。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# 允许上传的单请求总大小（默认 2GB）
MAX_CONTENT_LENGTH = int(os.environ.get("MAX_CONTENT_LENGTH", str(2 * 1024 ** 3)))

# 确定性 UID 派生所用组织前缀（必须是合法 DICOM UID 前缀，且派生结果 <= 64 字符）。
# 默认使用 IANA 测试弧 1.2.826.0.1，仅用于测试；生产部署必须替换成机构自己的合法前缀。
DEFAULT_UID_PREFIX = "1.2.826.0.1.3680043.10.9999"

# SQLite 写锁等待（毫秒），配合 WAL 支持多 worker 并发上传
SQLITE_BUSY_TIMEOUT_MS = int(os.environ.get("SQLITE_BUSY_TIMEOUT_MS", "30000"))


class _Paths:
    """延迟解析的数据目录视图，每次属性访问都反映当前环境变量。"""

    @property
    def data_dir(self) -> Path:
        return Path(os.environ.get("DICOM_DATA_DIR", "/data")).resolve()

    @property
    def db_path(self) -> Path:
        return self.data_dir / "dicom.db"

    @property
    def objects_dir(self) -> Path:
        return self.data_dir / "objects"

    @property
    def upload_tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def batches_dir(self) -> Path:
        return self.data_dir / "batches"


_paths = _Paths()
# 向后兼容的模块级名称（首次导入时快照；运行时请使用 get_* 函数）
DATA_DIR = _paths.data_dir
DB_PATH = _paths.db_path
OBJECTS_DIR = _paths.objects_dir
UPLOAD_TMP_DIR = _paths.upload_tmp_dir
BATCHES_DIR = _paths.batches_dir


def get_db_path() -> Path:
    return _paths.db_path


def get_objects_dir() -> Path:
    return _paths.objects_dir


def get_upload_tmp_dir() -> Path:
    return _paths.upload_tmp_dir


def get_batches_dir() -> Path:
    return _paths.batches_dir


def get_uid_prefix() -> str:
    prefix = os.environ.get("DICOM_UID_PREFIX", DEFAULT_UID_PREFIX)
    validate_uid_prefix(prefix)
    return prefix


def __getattr__(name):  # PEP 562：config.UID_PREFIX 动态读取环境变量
    if name == "UID_PREFIX":
        return get_uid_prefix()
    raise AttributeError(name)


def validate_uid_prefix(prefix: str) -> None:
    """校验 UID 前缀合法性，避免生成非法或超长 DICOM UID。"""
    if not prefix or not all(c.isdigit() for c in prefix.replace(".", "")):
        raise ValueError(f"DICOM_UID_PREFIX 非法（只能含数字与点）: {prefix!r}")
    parts = prefix.split(".")
    if any(part == "" or part[0] == "0" and len(part) > 1 for part in parts):
        raise ValueError(f"DICOM_UID_PREFIX 含空段或前导零段: {prefix!r}")
    # 前缀 + ".0."(3) + 31 位尾段 <= 64，故前缀最长 30 字符
    if len(prefix) > 30:
        raise ValueError("DICOM_UID_PREFIX 前缀过长，无法保证派生 UID <= 64 字符")


def ensure_dirs() -> None:
    for path in (
        _paths.data_dir,
        _paths.objects_dir,
        _paths.upload_tmp_dir,
        _paths.batches_dir,
    ):
        path.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------- 研究 PACS
@dataclass(frozen=True)
class PacsTarget:
    """C-STORE 接收方（研究 PACS）连接参数。"""

    ae_title: str          # 接收方 Called AE Title
    host: str
    port: int
    calling_ae_title: str  # 本服务 Calling AE Title

    @property
    def identity(self) -> str:
        """目标标识：送达记录按此键隔离，换目标即换一套送达视图。"""
        return f"{self.ae_title}@{self.host}:{self.port}"


def _validate_ae_title(env_name: str, value: str) -> str:
    if not value or len(value) > 16 or not value.isprintable():
        raise ValueError(f"{env_name} 非法（1-16 个可打印字符）: {value!r}")
    return value


def get_pacs_target() -> PacsTarget | None:
    """读取研究 PACS 配置；未设置 PACS_HOST 时返回 None（发送功能不可用）。"""
    host = os.environ.get("PACS_HOST", "").strip()
    if not host:
        return None
    ae_title = _validate_ae_title(
        "PACS_AE_TITLE",
        os.environ.get("PACS_AE_TITLE", "RESEARCH_PACS").strip() or "RESEARCH_PACS",
    )
    calling = _validate_ae_title(
        "PACS_CALLING_AE_TITLE",
        os.environ.get("PACS_CALLING_AE_TITLE", "DICOM_BATCH").strip() or "DICOM_BATCH",
    )
    port = int(os.environ.get("PACS_PORT", "11112"))
    if not 1 <= port <= 65535:
        raise ValueError(f"PACS_PORT 非法: {port}")
    return PacsTarget(ae_title=ae_title, host=host, port=port, calling_ae_title=calling)


def get_pacs_connect_timeout() -> float:
    """TCP 连接 / 关联协商超时（秒）。"""
    return float(os.environ.get("PACS_CONNECT_TIMEOUT", "10"))


def get_pacs_dimse_timeout() -> float:
    """等待 C-STORE 响应的 DIMSE 超时（秒）。"""
    return float(os.environ.get("PACS_DIMSE_TIMEOUT", "30"))


def get_send_claim_ttl() -> float:
    """发送认领的有效期（秒）：过期认领视为发送方已崩溃，可被重试接管。"""
    return float(os.environ.get("PACS_CLAIM_TTL_SECONDS", "300"))
