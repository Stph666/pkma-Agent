"""配置读取与派生属性测试。"""

from __future__ import annotations

from pathlib import Path

from app.config import BASE_DIR, Settings


def test_allowed_extensions_normalized():
    settings = Settings(allowed_extensions=" PDF, .Docx ,txt ,, ")
    assert settings.allowed_extension_set == {".pdf", ".docx", ".txt"}


def test_max_upload_bytes():
    assert Settings(max_upload_mb=20).max_upload_bytes == 20 * 1024 * 1024


def test_safe_database_url_hides_password():
    settings = Settings(
        database_url="mysql+pymysql://pkma:secret@127.0.0.1:3306/pkma?charset=utf8mb4"
    )
    safe = settings.safe_database_url()
    assert "secret" not in safe
    assert "pkma:***@" in safe


def test_llm_configured_requires_all_fields():
    assert Settings(llm_base_url="", llm_api_key="", llm_model="").llm_configured is False
    assert (
        Settings(llm_base_url="https://api.example.com/v1", llm_api_key="k", llm_model="m").llm_configured
        is True
    )


def test_relative_data_dir_resolves_under_project(tmp_path: Path):
    settings = Settings(data_dir="data")
    assert settings.resolved_data_dir == (BASE_DIR / "data").resolve()


def test_blank_upload_dir_falls_back_to_data_dir():
    settings = Settings(data_dir=str(BASE_DIR / "data"), upload_dir=None)
    assert settings.resolved_upload_dir == settings.resolved_data_dir / "uploads"


def test_chroma_dir_defaults_to_ascii_path():
    """向量目录默认落在纯英文路径：Chroma 在非 ASCII 路径下写大索引会丢数据。"""
    settings = Settings(chroma_dir=None)
    assert settings.resolved_chroma_dir == (Path.home() / ".pkma" / "chroma").resolve()
    assert str(settings.resolved_chroma_dir).isascii()


def test_chroma_dir_respects_explicit_setting(tmp_path: Path):
    settings = Settings(chroma_dir=tmp_path / "vectors")
    assert settings.resolved_chroma_dir == (tmp_path / "vectors").resolve()


def test_ensure_directories(tmp_path: Path):
    settings = Settings(data_dir=tmp_path / "d")
    settings.ensure_directories()
    assert settings.resolved_upload_dir.is_dir()
    assert settings.resolved_chroma_dir.is_dir()
    assert settings.samples_dir.is_dir()
    assert settings.evaluation_dir.is_dir()
