"""Tests for the CLI."""

from click.testing import CliRunner

from contextbridge.cli import main


class TestCLI:
    def test_version(self):
        runner = CliRunner()
        result = runner.invoke(main, ["--version"])
        assert result.exit_code == 0
        assert "0.1.0" in result.output

    def test_list_empty(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CB_STORAGE_DIR", str(tmp_path))
        runner = CliRunner()
        result = runner.invoke(main, ["list"])
        assert result.exit_code == 0

    def test_history_not_found(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CB_STORAGE_DIR", str(tmp_path))
        runner = CliRunner()
        result = runner.invoke(main, ["history", "nonexistent"])
        assert result.exit_code != 0

    def test_inspect_not_found(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CB_STORAGE_DIR", str(tmp_path))
        runner = CliRunner()
        result = runner.invoke(main, ["inspect", "nonexistent"])
        assert result.exit_code != 0

    def test_export_missing_file(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CB_STORAGE_DIR", str(tmp_path))
        runner = CliRunner()
        result = runner.invoke(
            main, ["export", "--model", "openai", "--chat", "nonexistent.txt", "--output", "test"]
        )
        assert result.exit_code != 0
