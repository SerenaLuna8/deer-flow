from fastapi import APIRouter, Depends

from app.gateway.deps import require_project_private_open
from app.gateway.private_work_schemas import PrivateWorkRoute
from app.gateway.routers.private_work_routes import (
    approvals,
    context_controls,
    feedback,
    files,
    readiness,
    runs,
    threads,
)

router = APIRouter(
    prefix="/api/projects/{project_id}/private-work",
    tags=["project-private-work"],
    route_class=PrivateWorkRoute,
    dependencies=[Depends(require_project_private_open)],
)
router.include_router(readiness.router)
router.include_router(context_controls.router)
router.include_router(files.router)
router.include_router(approvals.router)
router.include_router(runs.router)
router.include_router(feedback.router)
router.include_router(threads.router)
