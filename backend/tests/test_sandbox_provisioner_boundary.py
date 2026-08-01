from pathlib import Path


def test_public_nginx_routes_all_api_requests_to_gateway() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    nginx_config = (repo_root / "docker" / "nginx" / "nginx.conf").read_text()

    assert "location /api/sandboxes" not in nginx_config
    assert "provisioner:8002" not in nginx_config
    assert "location /api/" in nginx_config
