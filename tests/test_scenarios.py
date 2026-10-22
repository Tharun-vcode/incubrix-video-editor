from pathlib import Path

import yaml

from app.cli.session_runner import run_session


ROOT = Path(__file__).resolve().parents[1]
SESSION_DIR = ROOT / "scenarios" / "sessions"


def test_all_scripted_assessment_sessions_match_expected_statuses(tmp_path):
    session_files = sorted(SESSION_DIR.glob("*.yaml"))
    assert len(session_files) == 15
    step_count = 0

    for session_file in session_files:
        payload = yaml.safe_load(session_file.read_text(encoding="utf-8"))
        result = run_session(
            session_file,
            tmp_path / session_file.stem,
        )
        expected = [
            step["expected_status"]
            for step in payload["steps"]
        ]
        actual = [
            step["status"]
            for step in result["steps"]
        ]

        assert actual == expected, session_file.name
        step_count += len(actual)

    assert step_count == 45
