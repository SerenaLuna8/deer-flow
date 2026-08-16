from app.projects.capabilities import Capability, capabilities_for
from app.projects.models import ProjectRole


def test_project_role_capability_matrix_is_monotonic_for_public_members() -> None:
    admin = capabilities_for(ProjectRole.ADMIN)
    editor = capabilities_for(ProjectRole.EDITOR)
    runner = capabilities_for(ProjectRole.RUNNER)
    viewer = capabilities_for(ProjectRole.VIEWER)

    assert admin == frozenset(Capability)
    assert editor == runner | {Capability.SHARED_ASSETS_EDIT}
    assert viewer < runner < editor < admin

    assert Capability.SHARED_ASSETS_READ in runner
    assert Capability.SHARED_ASSETS_EXECUTE in runner
    assert Capability.SHARED_ASSETS_EDIT not in runner

    assert Capability.PRIVATE_WORK_CREATE not in viewer
    assert Capability.SHARED_ASSETS_EXECUTE not in viewer
    assert Capability.SHARED_ASSETS_EDIT not in viewer


def test_channel_guest_keeps_execution_without_project_navigation_authority() -> None:
    channel_guest = capabilities_for(ProjectRole.CHANNEL_GUEST)

    assert channel_guest == frozenset(
        {
            Capability.PRIVATE_WORK_CREATE,
            Capability.PRIVATE_WORK_READ_OWN,
            Capability.SHARED_ASSETS_READ,
            Capability.SHARED_ASSETS_EXECUTE,
        }
    )
    assert Capability.PROJECT_READ not in channel_guest
    assert Capability.PROJECT_ENTER not in channel_guest
