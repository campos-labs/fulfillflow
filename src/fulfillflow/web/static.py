"""Static-file response policy for package-owned, local assets."""

from os import PathLike

from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope


class FulfillFlowStaticFiles(StaticFiles):
    """Serve local assets with explicit MIME sniffing and bounded cache policy."""

    def __init__(self, *, directory: str | PathLike[str]) -> None:
        super().__init__(directory=directory, check_dir=True)

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["X-Content-Type-Options"] = "nosniff"
        normalized_path = path.replace("\\", "/")
        if normalized_path.startswith("vendor/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        else:
            response.headers["Cache-Control"] = "public, max-age=3600"
        return response
