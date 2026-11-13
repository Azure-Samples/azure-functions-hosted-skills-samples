from demo import validate


def _doc(**overrides):
    doc = {
        "date": "2026-05-04",
        "data_source": "office365",
        "top_priorities": [{"title": "Send the RCA", "why": "Overdue."}],
        "schedule": [{"time": "13:00-13:45", "title": "Fabrikam"}],
        "markdown": "# Daily work briefing - 2026-05-04\n",
    }
    doc.update(overrides)
    return doc


def test_validate_accepts_a_complete_briefing():
    assert validate(_doc(), "2026-05-04") == []


def test_validate_flags_missing_and_incomplete_briefings():
    assert validate(None, "2026-05-04") == ["no briefing published for 2026-05-04"]
    errors = validate(_doc(top_priorities=[{"title": "x", "why": ""}], schedule=[], markdown=""), "2026-05-05")
    assert errors == [
        "priority 1 has no reason",
        "schedule is empty",
        "markdown briefing missing",
        "briefing date '2026-05-04', expected 2026-05-05",
    ]
