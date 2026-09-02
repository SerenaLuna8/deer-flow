from __future__ import annotations

from collections import Counter
from itertools import chain

from fastapi.routing import APIRoute

from app.gateway.app import create_app
from app.gateway.routers.admin_assets import admin_project_router, admin_router
from app.gateway.routers.private_work import router as private_work_router
from app.gateway.routers.project_assets import catalog_router, project_router

_HTTP_METHODS = frozenset({"delete", "get", "head", "options", "patch", "post", "put", "trace"})


def _api_routes(router) -> list[APIRoute]:
    return [route for route in router.routes if isinstance(route, APIRoute)]


def _source_key(route: APIRoute) -> tuple[tuple[str, ...], str, str]:
    return tuple(sorted(route.methods or ())), route.path, route.name


def test_batch_2_routers_are_registered_once_without_global_duplicates() -> None:
    application = create_app()
    app_routes = _api_routes(application)
    app_source_keys = Counter(_source_key(route) for route in app_routes)
    source_routes = chain(
        _api_routes(private_work_router),
        _api_routes(project_router),
        _api_routes(catalog_router),
        _api_routes(admin_router),
        _api_routes(admin_project_router),
    )
    for route in source_routes:
        assert app_source_keys[_source_key(route)] == 1

    method_paths = Counter((method, route.path) for route in app_routes for method in route.methods or ())
    assert {key: count for key, count in method_paths.items() if count != 1} == {}

    route_ids = [route.unique_id for route in app_routes]
    assert all(route_ids)
    assert len(route_ids) == len(set(route_ids))

    schema = application.openapi()
    operation_ids = [operation["operationId"] for path_item in schema["paths"].values() for method, operation in path_item.items() if method in _HTTP_METHODS]
    assert all(operation_ids)
    assert len(operation_ids) == len(set(operation_ids))
