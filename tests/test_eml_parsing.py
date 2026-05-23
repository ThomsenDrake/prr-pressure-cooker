from email.message import EmailMessage

from prr_pressure_cooker.ingest import _parse_eml


def test_parse_eml_marks_requester_sender_as_human_message(tmp_path):
    message = EmailMessage()
    message["From"] = "Drake <drake.t98@proton.me>"
    message["To"] = "Public Record Unit <publicrecordunit@ocfl.net>"
    message["Subject"] = "Re: PRR- 163721"
    message["Date"] = "Mon, 11 May 2026 17:03:00 +0000"
    message.set_content("Following up on this PRR.")
    path = tmp_path / "sent.eml"
    path.write_bytes(message.as_bytes())

    parsed = _parse_eml(path)

    assert parsed["event_type"] == "human_sent_message"
