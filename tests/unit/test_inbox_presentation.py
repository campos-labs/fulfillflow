"""Legacy UI must not manufacture a v1.2 admission or persisted result."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from fulfillflow.web.responses import templates


@pytest.mark.parametrize("status", ["RECEIVED", "PROCESSED"])
def test_legacy_reception_has_no_invented_acceptance_result_or_polling(status):
    event = SimpleNamespace(
        id=uuid4(),
        external_event_id="legacy-event",
        carrier_code="carrier-alpha",
        status=status,
        progress=None,
        result=None,
        payload=None,
        received_at=None,
        processed_at=None,
        completed_at=None,
        error_code=None,
        error_detail=None,
        request_id=uuid4(),
    )
    html = templates.get_template("content/inbox_detail.html").render(
        event=event,
        request=None,
        path_for=lambda *args, **kwargs: "#",
    )
    assert "HTTP 202" not in html
    assert 'hx-trigger="observe"' not in html
    assert "The persisted result is shown below" not in html
    assert "Persisted result" not in html
    assert ("Legacy reception" if status == "RECEIVED" else "Completed.") in html
