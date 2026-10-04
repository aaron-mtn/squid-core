from squid_core.platform_context import detect_platform_context


def lookup_with(*available: str):
    return lambda name: f"C:\\Shells\\{name}.exe" if name in available else None


def test_windows_prefers_pwsh_and_uses_windows_paths() -> None:
    context = detect_platform_context("Windows", {}, lookup_with("pwsh", "powershell"))

    assert context.os_name == "Windows"
    assert context.shell_name == "PowerShell"
    assert context.shell_executable == "C:\\Shells\\pwsh.exe"
    assert context.path_separator == "\\"
    assert context.is_windows is True


def test_windows_falls_back_to_windows_powershell() -> None:
    context = detect_platform_context("Windows", {}, lookup_with("powershell"))

    assert context.shell_executable == "C:\\Shells\\powershell.exe"


def test_windows_has_deterministic_fallback_when_lookup_finds_nothing() -> None:
    context = detect_platform_context("Windows", {}, lookup_with())

    assert context.shell_executable == "powershell"


def test_linux_uses_shell_environment() -> None:
    context = detect_platform_context("Linux", {"SHELL": "/usr/bin/bash"})

    assert context.os_name == "Linux"
    assert context.shell_name == "bash"
    assert context.shell_executable == "/usr/bin/bash"
    assert context.path_separator == "/"
    assert context.is_windows is False


def test_linux_falls_back_to_sh() -> None:
    context = detect_platform_context("Linux", {})

    assert context.shell_name == "sh"
    assert context.shell_executable == "/bin/sh"


def test_macos_uses_shell_environment_and_zsh_fallback() -> None:
    detected = detect_platform_context("Darwin", {"SHELL": "/opt/homebrew/bin/fish"})
    fallback = detect_platform_context("Darwin", {})

    assert detected.os_name == "macOS"
    assert detected.shell_name == "fish"
    assert detected.path_separator == "/"
    assert fallback.shell_name == "zsh"
    assert fallback.shell_executable == "/bin/zsh"
