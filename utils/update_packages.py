import subprocess


def update_all_packages() -> None:
    """
    In uv, we don't need to loop through a text file.
    'uv lock --upgrade' handles the logic of finding the latest
    versions for everything defined in pyproject.toml.
    """
    print("Updating all packages to latest versions...")

    # 1. Upgrade the lockfile to the latest versions
    subprocess.run(["uv", "lock", "--upgrade"], check=True)  # noqa: S607

    # 2. Sync the virtual environment to match the new lockfile
    subprocess.run(["uv", "sync"], check=True)  # noqa: S607

    # 3. Get the new versions (similar to your old 'pip show' logic)
    # uv export is a fast way to see what the final resolved versions are
    subprocess.run(
        ["uv", "pip", "freeze"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    )

    print("\nUpdated Environment:")
    subprocess.run(["uv", "tree"], check=True)  # noqa: S607


def update_specific_package(package_name: str) -> None:
    """
    If you want to update just one package to latest.
    """
    subprocess.run(["uv", "add", f"{package_name}@latest"], check=True)  # noqa: S603, S607


if __name__ == "__main__":
    # This single call replaces your entire main() logic
    update_all_packages()
