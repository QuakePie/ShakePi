"""The ShakePie terminal console: menus, views, and CLI actions."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from contextlib import suppress
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from shakepie_terminal import recorder_settings
from shakepie_terminal.connection import SessionError, ShakePieSession
from shakepie_terminal.keyio import is_interactive
from shakepie_terminal.menu import Menu, MenuItem, MultiSelect, ToggleItem
from shakepie_terminal.names import (
    ARCHIVE_WEB_CONF,
    HEALTH_FILE,
    RECORDING_DIR,
    human_bytes,
)
from shakepie_terminal.profile import Profile
from shakepie_terminal.recordings import (
    DayGroup,
    delete_day_files,
    delete_dayplots,
    download_dayplots,
    download_days,
    export_miniseed_days,
    list_dayplot_days,
    list_recording_days,
)

SERVICES = (
    "seisberry-recorder.service",
    "shakepie-archive-web.service",
    "shakepie-dayplot-worker.service",
)

console = Console()


def _connect(profile: Profile, password: str | None) -> ShakePieSession:
    session = ShakePieSession(profile.host, profile.port, profile.user, password)
    with console.status(f"[bold]Connecting to {profile.user}@{profile.host}…[/bold]"):
        session.connect()
    return session


def _prompt_password(profile: Profile, *, sudo_hint: bool = False) -> str:
    message = f"SSH password for {profile.user}@{profile.host}"
    if sudo_hint:
        message += " (also used for sudo on the recorder)"
    return getpass.getpass(message + ": ")


def _run_sudo(session: ShakePieSession, command: str) -> str:
    """Run a root command, prompting for the sudo password once if needed."""
    try:
        return session.run_sudo(command).stdout
    except SessionError as error:
        if "password" in str(error).lower():
            password = getpass.getpass("sudo password on the recorder: ")
            session.password = password
            return session.run_sudo(command).stdout
        raise


def _pause() -> None:
    console.print("\n[dim]Press Enter to continue…[/dim]", end="")
    with suppress(EOFError):
        input()
    console.clear()


def _detector_minimum_components(session: ShakePieSession) -> int:
    """The archive web's N-of-M detector gate (1 = legacy vertical-only trigger)."""
    query = session.run(f"grep 'minimum_components' {ARCHIVE_WEB_CONF} 2>/dev/null | tail -1")
    text = query.stdout.strip().split("=")[-1].strip() if query.ok() else ""
    try:
        return max(1, min(3, int(text)))
    except ValueError:
        return 1


# ---------------------------------------------------------------------------
# views


def _status_view(session: ShakePieSession) -> None:
    console.clear()
    health = session.run(f"cat {HEALTH_FILE}")
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    if health.ok():
        try:
            data = json.loads(health.stdout)
            labels = (
                ("state", "Recorder state"),
                ("fatal_code", "Fatal code"),
                ("nominal_group_rate_hz", "Group rate (Hz)"),
                ("acquired_groups", "Acquired groups"),
                ("missing_sample_groups", "Missing groups"),
                ("simulated_sequence_gaps", "Sequence gaps"),
                ("storage_errors", "Storage errors"),
                ("disk_queue_depth", "Disk queue depth"),
                ("connected_clients", "Live clients"),
                ("disk_free_bytes", "Disk free (recorder)"),
            )
            for key, label in labels:
                value = data.get(key, "—")
                table.add_row(label, f"{value:,}" if isinstance(value, int) else str(value))
        except json.JSONDecodeError:
            table.add_row("health.json", "unreadable")
    else:
        table.add_row("Recorder", "health file unavailable (recorder stopped?)")
    if health.ok():
        try:
            calibration_data = json.loads(health.stdout)
            offsets = ", ".join(
                f"{axis} {calibration_data.get(f'zero_offset_{axis}', 0):,}"
                for axis in ("north", "east", "vertical")
            )
            table.add_row(
                "Zero offsets",
                f"{offsets} counts · {calibration_data.get('calibration_phase', 'idle')}",
            )
        except json.JSONDecodeError:
            pass

    components = _detector_minimum_components(session)
    table.add_row(
        "Event trigger",
        (
            f"N-of-M coincidence · at least {components} of 3 components"
            if components >= 2
            else "vertical channel only"
        ),
    )

    disks = session.run("df -h /mnt/shakepie-data | tail -1")
    if disks.ok():
        fields = disks.stdout.split()
        if len(fields) >= 6:
            table.add_row("HDD", f"{fields[2]} used · {fields[4]} used · {fields[1]} total")
    load = session.run("uptime -p")
    if load.ok():
        table.add_row("Machine", load.stdout.strip() or "—")
    temperature = session.run("cat /sys/class/thermal/thermal_zone0/temp")
    if temperature.ok() and temperature.stdout.strip().isdigit():
        table.add_row("SoC temperature", f"{int(temperature.stdout.strip()) / 1000:.1f} °C")
    for service in SERVICES:
        state = session.run(f"systemctl is-active {service}")
        marker = "●" if state.stdout.strip() == "active" else "○"
        table.add_row(f"Service {service.split('.')[0]}", f"{marker} {state.stdout.strip() or '?'}")
    console.print(Panel(table, title=f"ShakePie · {session.host}", border_style="cyan"))
    _pause()


def _storage_view(session: ShakePieSession) -> None:
    console.clear()
    usage = session.run_sudo(f"du -sh {RECORDING_DIR} 2>/dev/null | cut -f1")
    recordings_size = usage.stdout.strip() or "—"
    plots = session.run("du -sh /mnt/shakepie-data/dayplots 2>/dev/null | cut -f1")
    free = session.run("df -h /mnt/shakepie-data | tail -1")
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    table.add_row("Recordings", recordings_size)
    table.add_row("Dayplots", plots.stdout.strip() or "—")
    if free.ok():
        fields = free.stdout.split()
        if len(fields) >= 6:
            table.add_row("HDD free", f"{fields[3]} of {fields[1]} ({fields[4]} used)")
    days = list_recording_days(session)
    table.add_row("Days with data", str(len(days)))
    table.add_row("Total finalized size", human_bytes(sum(day.total_bytes for day in days)))
    console.print(Panel(table, title="Storage summary", border_style="cyan"))
    _pause()


# ---------------------------------------------------------------------------
# recordings


def _choose_days(
    days: list[DayGroup], action: str, downloaded: set[str] | None = None
) -> list[DayGroup]:
    by_day = {day.day: day for day in days}
    items = [ToggleItem(day.label(downloaded), day.day, checked=False) for day in days]

    def summary(toggles: list[ToggleItem]) -> str:
        chosen = [toggle.value for toggle in toggles if toggle.checked]
        size = sum(by_day[value].total_bytes for value in chosen)
        return f"Selected: {len(chosen)} day(s) · {human_bytes(size)}"

    picker = MultiSelect(
        f"Select days to {action}",
        items,
        console=console,
        subtitle="Space toggles · a selects all · n clears · Enter confirms",
        summary=summary,
    )
    chosen = picker.run()
    if chosen is None:
        return []
    return [by_day[value] for value in chosen]


def _choose_channel() -> str | None:
    """Pick the component(s) for a MiniSEED export; None when cancelled."""
    menu = Menu(
        "MiniSEED channels",
        [
            MenuItem("All components (EHN + EHE + EHZ)", "all"),
            MenuItem("North only (EHN)", "north"),
            MenuItem("East only (EHE)", "east"),
            MenuItem("Vertical only (EHZ)", "vertical"),
            MenuItem("Cancel", "cancel"),
        ],
        console=console,
    )
    choice = menu.run()
    return None if choice in {None, "cancel"} else choice


def _recordings_menu(session: ShakePieSession, profile: Profile) -> None:
    while True:
        days = list_recording_days(session)
        if not days:
            console.print("[yellow]No finalized recordings found on the recorder.[/yellow]")
            _pause()
            return
        menu = Menu(
            "Recordings",
            [
                MenuItem(
                    "Download days (batch, raw .spb)",
                    "download",
                    hint=f"{len(days)} days · {human_bytes(sum(d.total_bytes for d in days))}",
                ),
                MenuItem("Download days as MiniSEED (for ObsPy/Swarm)", "download-mseed"),
                MenuItem("Download everything not yet offloaded", "download-new"),
                MenuItem("Delete days", "delete", danger=True),
                MenuItem("Back", "back"),
            ],
            console=console,
            subtitle=f"{len(days)} UTC days · destination {profile.local_dir}",
        )
        choice = menu.run()
        if choice in {None, "back"}:
            return
        if choice == "download":
            chosen = _choose_days(days, "download")
            if not chosen:
                continue
            destination = Path(profile.local_dir)
            done, skipped, failed = download_days(session, chosen, destination, console=console)
            console.print(
                f"[green]Downloaded {done}[/green] · skipped {skipped} "
                f"(already present) · failed {failed} → {destination}"
            )
            profile.save()
            _pause()
        elif choice == "download-mseed":
            chosen = _choose_days(days, "download as MiniSEED")
            if not chosen:
                continue
            channel = _choose_channel()
            if channel is None:
                continue
            destination = Path(profile.local_dir)
            console.print(
                "Converting on the recorder and downloading — raw files are not touched."
            )
            done, skipped, failed = export_miniseed_days(
                session, chosen, destination, channel=channel, console=console
            )
            console.print(
                f"[green]Converted {done}[/green] · skipped {skipped} "
                f"(already present) · failed {failed} → {destination}"
            )
            profile.save()
            _pause()
        elif choice == "download-new":
            destination = Path(profile.local_dir)
            local_days = {
                path.name
                for path in destination.iterdir()
                if path.is_dir()
            } if destination.is_dir() else set()
            fresh = [day for day in days if day.day not in local_days]
            if not fresh:
                console.print("[green]Everything is already offloaded.[/green]")
                _pause()
                continue
            done, skipped, failed = download_days(session, fresh, destination, console=console)
            console.print(
                f"[green]Downloaded {done}[/green] · skipped {skipped} · failed {failed} "
                f"from {len(fresh)} new day(s)"
            )
            _pause()
        elif choice == "delete":
            chosen = _choose_days(days, "DELETE")
            if not chosen:
                continue
            total = sum(day.total_bytes for day in chosen)
            console.print(
                Panel(
                    Text(
                        "Deleted recordings cannot be recovered. The archive web catalog "
                        "will reconcile the removal on its next run.",
                        style="bold red",
                    ),
                    title="Permanent deletion",
                )
            )
            answer = console.input(
                f"Type DELETE to permanently remove {len(chosen)} day(s) "
                f"({human_bytes(total)}): "
            )
            if answer.strip() != "DELETE":
                console.print("[green]Cancelled.[/green]")
                _pause()
                continue
            removed = 0
            freed = 0
            for day in chosen:
                removed += delete_day_files(session, day, require_confirmation=lambda _day: True)
                freed += day.total_bytes
            console.print(f"[green]Removed {removed} files · {human_bytes(freed)} freed.[/green]")
            _pause()


def _dayplots_menu(session: ShakePieSession, profile: Profile) -> None:
    while True:
        days = list_dayplot_days(session)
        if not days:
            console.print("[yellow]No dayplots are cached on the recorder.[/yellow]")
            _pause()
            return
        menu = Menu(
            "Dayplots",
            [
                MenuItem("Download dayplots (batch)", "download"),
                MenuItem("Delete dayplots", "delete", danger=True),
                MenuItem("Back", "back"),
            ],
            console=console,
            subtitle=f"{len(days)} days cached",
        )
        choice = menu.run()
        if choice in {None, "back"}:
            return
        chosen = _choose_days(days, choice)
        if not chosen:
            continue
        destination = Path(profile.local_dir)
        if choice == "download":
            done, skipped, failed = download_dayplots(session, chosen, destination)
            console.print(
                f"[green]Downloaded {done}[/green] · skipped {skipped} · failed {failed} "
                f"→ {destination / 'dayplots'}"
            )
        else:
            answer = console.input(f"Type DELETE to remove dayplots of {len(chosen)} day(s): ")
            if answer.strip() != "DELETE":
                console.print("[green]Cancelled.[/green]")
            else:
                removed = sum(delete_dayplots(session, day.day) for day in chosen)
                console.print(f"[green]Removed {removed} plot images.[/green]")
        _pause()


# ---------------------------------------------------------------------------
# settings


def _settings_menu(session: ShakePieSession) -> None:
    try:
        original_text, current = recorder_settings.fetch(session)
    except recorder_settings.SettingsError as error:
        console.print(f"[red]{error}[/red]")
        _pause()
        return
    while True:
        console.clear()
        console.print(Panel(recorder_text(current), title="Recorder settings", border_style="cyan"))
        menu = Menu(
            "Change a setting",
            [
                MenuItem("ADC conversion rate (adc.data_rate_sps)", "adc.data_rate_sps"),
                MenuItem(
                    "Sample-group rate (acquisition.nominal_group_rate_hz)",
                    "acquisition.nominal_group_rate_hz",
                ),
                MenuItem(
                    "Groups per block (acquisition.samples_per_block)",
                    "acquisition.samples_per_block",
                ),
                MenuItem("Max consecutive ADC errors", "acquisition.max_consecutive_adc_errors"),
                MenuItem(
                    "File rotation minutes (storage.rotation_minutes)",
                    "storage.rotation_minutes",
                ),
                MenuItem("Station ID (storage.station_id)", "storage.station_id"),
                MenuItem("Calibrate sensor zero (measure and recenter data)", "calibrate"),
                MenuItem("Record raw again (clear zero offsets)", "clear-zero", danger=True),
                MenuItem("Restart recorder service", "restart", danger=True),
                MenuItem("Back (discard pending changes)", "back"),
            ],
            console=console,
            subtitle="Enter edits a value · changes apply one at a time from this menu",
        )
        choice = menu.run()
        if choice in {None, "back"}:
            return
        if choice == "calibrate":
            console.print(
                "Calibrating measures each channel for about ten seconds while recording "
                "continues, then records around the measured zero. Keep the ground quiet."
            )
            if console.input("Start zero calibration now? [y/N]: ").lower() == "y":
                try:
                    recorder_settings.request_zero_calibration(session, "calibrate")
                    console.print(
                        "[green]Calibration requested. Check Status in a few seconds "
                        "for the applied offsets.[/green]"
                    )
                except (recorder_settings.SettingsError, SessionError) as error:
                    console.print(f"[red]{error}[/red]")
            _pause()
            continue
        if choice == "clear-zero":
            if console.input(
                "Stop subtracting offsets and record raw counts again? [y/N]: "
            ).lower() == "y":
                try:
                    recorder_settings.request_zero_calibration(session, "clear")
                    console.print("[green]Clear requested.[/green]")
                except (recorder_settings.SettingsError, SessionError) as error:
                    console.print(f"[red]{error}[/red]")
            _pause()
            continue
        if choice == "restart":
            if console.input("Restart seisberry-recorder.service now? [y/N]: ").lower() == "y":
                try:
                    output = _run_sudo(session, "systemctl restart seisberry-recorder.service")
                    console.print(f"[green]Restarted. {output}[/green]")
                except SessionError as error:
                    console.print(f"[red]{error}[/red]")
            _pause()
            continue
        prompt = f"New value for {choice} [current: {current.get(choice, '')}]: "
        new_value = console.input(prompt).strip()
        updates = {choice: new_value}
        errors = recorder_settings.validate_values(current, updates)
        if errors:
            for message in errors:
                console.print(f"[red]{message}[/red]")
            _pause()
            continue
        preview = recorder_settings.render_recorder_conf(original_text, updates)
        console.print(Panel(preview, title="Proposed configuration", border_style="yellow"))
        if console.input("Apply and restart the recorder? [y/N]: ").lower() != "y":
            console.print("[green]Cancelled.[/green]")
            _pause()
            continue
        try:
            console.print("[green]" + recorder_settings.apply(session, updates) + "[/green]")
            original_text, current = recorder_settings.fetch(session)
        except (recorder_settings.SettingsError, SessionError) as error:
            console.print(f"[red]{error}[/red]")
        _pause()


def recorder_text(current: dict[str, str]) -> Text:
    text = Text()
    for key in recorder_settings.EDITABLE_KEYS:
        text.append(f"{key} ", style="bold")
        text.append(f"= {current.get(key, '?')}\n")
    return text


# ---------------------------------------------------------------------------
# logs


def _logs_menu(session: ShakePieSession) -> None:
    while True:
        menu = Menu(
            "Logs",
            [MenuItem(service, service) for service in SERVICES]
            + [MenuItem("Back", "back")],
            console=console,
        )
        choice = menu.run()
        if choice in {None, "back"}:
            return
        console.clear()
        result = session.run(f"journalctl -u {choice} -n 60 --no-pager -o short")
        body = result.stdout.strip() or result.stderr.strip() or "(no output)"
        console.print(Panel(body[-20_000:], title=choice, border_style="cyan"))
        _pause()


# ---------------------------------------------------------------------------
# main menu


def _main_menu(session: ShakePieSession, profile: Profile) -> None:
    while True:
        menu = Menu(
            f"ShakePie console · {session.user}@{session.host}",
            [
                MenuItem("Status and health", "status"),
                MenuItem("Recordings: offload, batch download, delete", "recordings"),
                MenuItem("Dayplots: download, delete", "dayplots"),
                MenuItem("Storage summary", "storage"),
                MenuItem("Recorder settings", "settings"),
                MenuItem("Logs", "logs"),
                MenuItem("Quit", "quit"),
            ],
            console=console,
        )
        choice = menu.run()
        if choice in {None, "quit"}:
            return
        try:
            if choice == "status":
                _status_view(session)
            elif choice == "recordings":
                _recordings_menu(session, profile)
            elif choice == "dayplots":
                _dayplots_menu(session, profile)
            elif choice == "storage":
                _storage_view(session)
            elif choice == "settings":
                _settings_menu(session)
            elif choice == "logs":
                _logs_menu(session)
        except SessionError as error:
            console.print(f"[red]Connection problem: {error}[/red]")
            _pause()
            return


def run_tui(profile: Profile, password: str | None) -> int:
    try:
        session = _connect(profile, password)
    except SessionError as error:
        console.print(f"[red]{error}[/red]")
        return 69
    try:
        console.print(f"[green]Connected to {profile.user}@{profile.host}.[/green]")
        _main_menu(session, profile)
    finally:
        session.close()
    return 0


# ---------------------------------------------------------------------------
# scripted actions (automation-friendly, no menus)


def _scripted_session(profile: Profile) -> ShakePieSession:
    password = os.environ.get("SHAKEPIE_TERMINAL_PASSWORD") or None
    if password is None and sys.stdin.isatty():
        password = _prompt_password(profile)
    return _connect(profile, password)


def run_scripted(profile: Profile, arguments: argparse.Namespace) -> int:
    try:
        with _scripted_session(profile) as session:
            if arguments.action == "status":
                health = session.run(f"cat {HEALTH_FILE}")
                if health.ok():
                    console.print_json(health.stdout)
                else:
                    console.print("health file unavailable")
                return 0
            if arguments.action in {"calibrate", "clear-zero"}:
                mode = "clear" if arguments.action == "clear-zero" else "calibrate"
                try:
                    recorder_settings.request_zero_calibration(session, mode)
                except (recorder_settings.SettingsError, SessionError) as error:
                    console.print(f"[red]{error}[/red]")
                    return 1
                console.print(
                    f"[green]{mode} requested. The recorder consumes the request within "
                    "a second; check status for the applied offsets.[/green]"
                )
                return 0
            if arguments.action == "list-days":
                for day in list_recording_days(session):
                    console.print(
                        f"{day.day}  {len(day.files)} files  {human_bytes(day.total_bytes)}"
                    )
                return 0
            if arguments.action == "download-days":
                days = list_recording_days(session)
                wanted = set(filter(None, arguments.days.split(",")))
                chosen = [day for day in days if day.day in wanted]
                if not chosen:
                    console.print("[yellow]No matching days on the recorder.[/yellow]")
                    return 1
                done, skipped, failed = download_days(
                    session,
                    chosen,
                    Path(arguments.local_dir or profile.local_dir),
                    verify=profile.verify,
                    console=console,
                )
                console.print(f"downloaded={done} skipped={skipped} failed={failed}")
                return 0 if failed == 0 else 1
            if arguments.action == "download-miniseed":
                days = list_recording_days(session)
                wanted = set(filter(None, arguments.days.split(",")))
                chosen = [day for day in days if day.day in wanted]
                if not chosen:
                    console.print("[yellow]No matching days on the recorder.[/yellow]")
                    return 1
                done, skipped, failed = export_miniseed_days(
                    session,
                    chosen,
                    Path(arguments.local_dir or profile.local_dir),
                    channel=arguments.channel,
                    console=console,
                )
                console.print(f"converted={done} skipped={skipped} failed={failed}")
                return 0 if failed == 0 else 1
            console.print(f"[red]Unknown action {arguments.action}[/red]")
            return 64
    except SessionError as error:
        console.print(f"[red]{error}[/red]")
        return 69


# ---------------------------------------------------------------------------
# entry point


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="shakepie-terminal",
        description=(
            "Terminal console for the ShakePie recorder: offload data, manage days, "
            "change settings over SSH."
        ),
    )
    parser.add_argument(
        "--host", help="recorder hostname or IP (default from ~/.shakepie-terminal.json)"
    )
    parser.add_argument("--port", type=int, help="SSH port")
    parser.add_argument("--user", help="SSH user")
    parser.add_argument("--local-dir", help="default download directory")
    parser.add_argument(
        "--action",
        choices=(
            "status",
            "list-days",
            "download-days",
            "download-miniseed",
            "calibrate",
            "clear-zero",
        ),
        help="run one action non-interactively (automation); omit for the interactive console",
    )
    parser.add_argument("--days", help="comma-separated UTC days for --action download-days")
    parser.add_argument(
        "--channel",
        choices=("all", "north", "east", "vertical"),
        default="all",
        help="component selection for --action download-miniseed",
    )
    parser.add_argument("--yes", action="store_true", help="assume yes for scripted prompts")
    arguments = parser.parse_args(argv)

    profile = Profile.load()
    if arguments.host:
        profile.host = arguments.host
    if arguments.port:
        profile.port = arguments.port
    if arguments.user:
        profile.user = arguments.user
    if arguments.local_dir:
        profile.local_dir = arguments.local_dir

    if arguments.action is None:
        if not is_interactive():
            console.print(
                "[red]The interactive console needs a terminal; use --action for scripting.[/red]"
            )
            return 64
        password = os.environ.get("SHAKEPIE_TERMINAL_PASSWORD") or _prompt_password(profile)
        return run_tui(profile, password)
    return run_scripted(profile, arguments)


if __name__ == "__main__":
    sys.exit(main())
