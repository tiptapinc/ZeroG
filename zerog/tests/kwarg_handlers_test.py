import json
import pdb
import pytest

from ..handlers.progress import ProgressHandler, UUID_PATT
from ..handlers.run_job import RunJobHandler, JOB_TYPE_PATT
from ..jobs import BaseJob
from ..registry import find_subclasses

from . import job_classes


# These tests vary from the "arg_handlers" tests only in the endpoint
# patterns, which test different paths to extract the uuid in the handler
#
# They need their own separate file because of limitations in the 'app'
# fixture

@pytest.fixture
def app(zerog_app):
    jobClasses = find_subclasses(BaseJob)
    handlers = [
        ("/progress/%s" % UUID_PATT, ProgressHandler),
        ("/runjob/%s" % JOB_TYPE_PATT, RunJobHandler)
    ]
    app = zerog_app(jobClasses, handlers)
    return app


@pytest.mark.gen_test
def test_progress(app, http_client, base_url):
    job = app.registry.make_job(dict(), job_classes.GoodJob.JOB_TYPE)
    job.save()
    response = yield http_client.fetch("%s/progress/%s" % (base_url, job.uuid))

    assert response.code == 200

    progress = json.loads(response.body)
    for key in ["completeness", "result", "events", "errors", "warnings"]:
        assert key in progress


@pytest.mark.gen_test
def test_run_job(app, http_client, base_url):
    response = yield http_client.fetch(
        (
            "%s/runjob/%s" %
            (base_url, job_classes.GoodJob.JOB_TYPE)
        ),
        method="POST",
        body=json.dumps({})
    )

    assert response.code == 201
    assert "uuid" in json.loads(response.body)
