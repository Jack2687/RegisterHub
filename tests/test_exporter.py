"""
Unit tests for universal account export engine.
"""
import json
import csv
from pathlib import Path
from core.exporter import parse_accounts_file, export_to_csv, export_to_json, export_to_bitwarden


def test_export_pipeline(tmp_path):
    # 1. Create a mock accounts.txt
    mock_file = tmp_path / "accounts.txt"
    mock_file.write_text("alex_miller1@test.com----Secret123!----alex_miller1\n", encoding="utf-8")

    accounts = parse_accounts_file(mock_file)
    assert len(accounts) == 1
    assert accounts[0]["email"] == "alex_miller1@test.com"
    assert accounts[0]["password"] == "Secret123!"

    # 2. Test CSV Export
    csv_out = tmp_path / "out.csv"
    export_to_csv(accounts, csv_out)
    assert csv_out.exists()
    with open(csv_out, "r", encoding="utf-8") as f:
        reader = list(csv.DictReader(f))
        assert len(reader) == 1
        assert reader[0]["email"] == "alex_miller1@test.com"

    # 3. Test JSON Export
    json_out = tmp_path / "out.json"
    export_to_json(accounts, json_out)
    assert json_out.exists()
    with open(json_out, "r", encoding="utf-8") as f:
        data = json.load(f)
        assert data["metadata"]["total_accounts"] == 1
        assert data["accounts"][0]["username"] == "alex_miller1"

    # 4. Test Bitwarden Export
    bw_out = tmp_path / "bitwarden.csv"
    export_to_bitwarden(accounts, bw_out)
    assert bw_out.exists()
    assert not csv_out.with_suffix(".csv.tmp").exists()
    assert not json_out.with_suffix(".json.tmp").exists()
    assert not bw_out.with_suffix(".csv.tmp").exists()
    with open(bw_out, "r", encoding="utf-8") as f:
        bw_reader = list(csv.DictReader(f))
        assert len(bw_reader) == 1
        assert bw_reader[0]["login_username"] == "alex_miller1@test.com"
        assert bw_reader[0]["login_password"] == "Secret123!"

    # 5. Backward compatibility with 4-column records (with api_key)
    mock_file_4col = tmp_path / "accounts4.txt"
    mock_file_4col.write_text("u@test.com----P@ss1----uname----sk-live-1234567890\n", encoding="utf-8")
    parsed4 = parse_accounts_file(mock_file_4col)
    assert len(parsed4) == 1
    assert parsed4[0]["email"] == "u@test.com"
    assert parsed4[0]["username"] == "uname"
