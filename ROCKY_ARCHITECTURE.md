# Rocky Runtime — System Architecture

**Platform:** Orange Pi Zero 2W  
**Display:** Waveshare 2.13-inch e-paper HAT V4  
**Runtime root:** `/opt/zero2w-manager`  
**Application root:** `/opt/rocky/apps`  
**System service:** `zero2w-manager.service`  
**CLI version:** Rocky CLI 0.2.0

---

## 1. System Overview

Rocky Runtime is a lightweight embedded application environment for the
Orange Pi Zero 2W.

It provides:

- An e-paper application launcher
- Application discovery through manifests
- Foreground-process lifecycle management
- Universal hardware-button input
- A Python application SDK
- Runtime health and application validation tools
- A foundation for future AI-agent integration

Rocky runs on Linux and does not replace the operating system. It acts as the
device's user-facing runtime and application-management layer.

The system is designed to remain:

- Lightweight
- Modular
- Offline-capable
- Hardware-aware
- Easy for humans and AI agents to extend
- Recoverable after application failure

---

## 2. High-Level Architecture

    Orange Pi Zero 2W
            |
            v
    Linux Kernel and systemd
            |
            v
    zero2w-manager.service
            |
            v
    Rocky Runtime
      |
      +-- Display Manager
      +-- Button Service
      +-- Button Server
      +-- Application Manager
      +-- Plugin Manager
      +-- Service Catalog
      +-- Menu Service
      +-- Rocky SDK
      +-- Rocky CLI
            |
            v
    Installed Rocky Applications

Rocky Runtime owns shared hardware and lifecycle management.

Applications operate through Rocky interfaces rather than directly taking
control of shared hardware.

---

## 3. Core Responsibilities

### Rocky Runtime owns

- E-paper display access
- Launcher rendering
- Physical button input
- Button-event distribution
- Application discovery
- Application launch and termination
- Foreground application state
- Process monitoring
- Menu restoration
- Runtime state persistence
- Service health

### Applications own

- Application-specific state
- Application-specific interface rendering
- Application behavior
- Handling SDK-delivered button events
- Releasing application-specific resources during cleanup

### Applications must not own

- Raw LRADC button devices
- Shared GPIO input devices
- Rocky's button socket
- Launcher state
- Runtime process state
- Direct control of another Rocky application

---

## 4. Repository Layout

Primary runtime location:

    /opt/zero2w-manager/

Important directories:

    manager/
        daemon.py
        application_manager.py
        button_service.py
        menu_service.py

        runtime/
            Application lifecycle services
            Button socket protocol
            Button server and client

        sdk/
            RockyApp
            RockyButtonApp
            Application-facing interfaces

        cli/
            main.py
            new_app.py
            apps.py
            validate.py
            doctor.py

        display/
            Display drivers and display abstractions

        plugins/
            Plugin discovery and manifest handling

        services/
            Runtime service implementations

Installed applications:

    /opt/rocky/apps/<application-id>/

Runtime-created files:

    /run/rocky/buttons.sock

Persistent runtime state may be stored under Rocky's configured runtime or
state directory.

---

## 5. Runtime Components

### 5.1 Manager Daemon

The manager daemon is Rocky's primary long-running process.

Responsibilities:

- Initialize runtime services
- Start the button server
- Start the physical button service
- Discover installed applications
- Render the launcher
- Route launcher navigation
- Launch selected applications
- Return to the launcher after application exit
- Shut down runtime services cleanly

The daemon is managed by:

    zero2w-manager.service

---

### 5.2 Display Manager

The Display Manager owns access to the Waveshare e-paper display.

Responsibilities:

- Initialize the display
- Render launcher and application content
- Perform partial refreshes
- Perform full refreshes
- Suppress unnecessary duplicate refreshes
- Release display resources
- Close the display safely

Only one runtime-controlled display owner should access the e-paper hardware
at a time.

---

### 5.3 Menu Service

The Menu Service represents the launcher interface.

Responsibilities:

- Store the current menu selection
- Render discovered applications
- Advance selection when Navigate is pressed
- Request launch when Select is pressed
- Restore the launcher after an application exits

Launcher order is determined by each application's `menu_order` value.

Applications with `menu_visible` set to false are not displayed in the
launcher.

---

### 5.4 Application Manager

The Application Manager controls foreground application processes.

Responsibilities:

- Launch applications
- Prevent conflicting foreground launches
- Create and monitor process groups
- Track the current application
- Persist runtime application state
- Recover from stale state
- Request graceful termination
- Escalate to forced termination when necessary
- Report application exits
- Restore launcher control after exit

The Application Manager is the authority for foreground-application state.

Applications must not launch or stop other applications directly.

---

### 5.5 Plugin Manager and Service Catalog

The plugin system discovers applications from the Rocky application root.

Default application location:

    /opt/rocky/apps/

Each application is represented by a directory containing a manifest.

The plugin system:

- Finds application directories
- Reads `manifest.json`
- Validates required metadata
- Resolves plugin-relative paths
- Builds the launcher catalog
- Supplies launch configuration to the Application Manager

---

### 5.6 Button Service

The Button Service reads the Orange Pi expansion-board buttons through the
Linux input subsystem.

Current logical controls:

- Navigate
- Select
- Long Select

Current behavior:

- Navigate short press moves through the launcher or is forwarded to the
  foreground application.
- Select short press launches the selected menu item or is forwarded to the
  foreground application.
- Select long press acts as the universal Home or Stop gesture.

Applications do not read `/dev/input/event*` directly.

---

### 5.7 Button Server

The Button Server distributes button events to foreground applications.

Socket:

    /run/rocky/buttons.sock

Architecture:

    Physical buttons
          |
          v
    ButtonService
          |
          v
    ButtonServer
          |
          v
    Unix domain socket
          |
          v
    ButtonClient
          |
          v
    RockyButtonApp

The Unix socket is used because foreground applications run in separate
processes and cannot consume the daemon's in-memory event bus directly.

---

### 5.8 Rocky SDK

The SDK provides application-facing runtime abstractions.

Base application types:

- `RockyApp`
- `RockyButtonApp`

`RockyApp` provides the basic lifecycle.

`RockyButtonApp` extends the lifecycle with automatic connection to Rocky's
button socket.

Typical application lifecycle:

    setup()
       |
       v
    update()
       |
       v
    update()
       |
       v
    cleanup()

A button-enabled application may also implement:

    on_button(event)

Applications should perform short, non-blocking work inside update and button
handlers whenever possible.

---

## 6. Universal Button Behavior

### Launcher active

- Button 1 short press: move up through applications
- Button 2 short press: move down through applications
- Long hold on either button: open the selected application or trigger the selected launcher action

### Foreground application active

- Button 1 short press: delivered to the application as `up / short_press`
- Button 2 short press: delivered to the application as `down / short_press`
- Long hold on either button: delivered to the application as `select / long_press`
- Extra-long hold: reserved runtime stop/home action
Extra-long hold remains controlled by Rocky Runtime even when an application
receives the event.

Current thresholds:
- `short_press`: under `0.9s`
- `long_press`: about `0.9s+`
- `very_long_press`: about `3.6s+`

Applications must not disable the universal Home behavior.

---

## 7. Application Structure

Each Rocky application normally contains:

    /opt/rocky/apps/<application-id>/
        manifest.json
        main.py
        README.md

### `manifest.json`

The manifest describes how Rocky discovers and launches the application.

Common fields:

- `id`
- `name`
- `description`
- `command`
- `working_directory`
- `environment`
- `menu_order`
- `menu_visible`
- `configured`
- `sdk`

Example responsibilities:

- `id`: stable machine-readable identifier
- `name`: launcher display name
- `command`: application launch command
- `working_directory`: process working directory
- `environment`: application environment variables
- `menu_order`: launcher sorting order
- `menu_visible`: whether the app appears in the launcher
- `sdk.buttons`: whether the app expects Rocky button events

The application directory name should match the manifest ID.

---

## 8. Application Creation

Rocky applications are generated with:

    rocky new app "Application Name"

Button support is enabled by default.

Useful options:

    --id
    --description
    --order
    --hidden
    --no-buttons
    --force

Compatibility command:

    rocky-new-app "Application Name"

The compatibility command delegates to:

    rocky new app

Generated applications should be validated before the runtime is restarted.

---

## 9. Rocky CLI

The Rocky CLI is the supported administration and development interface.

Current commands:

    rocky new app "Name"
    rocky apps
    rocky apps --all
    rocky apps --all --json
    rocky validate <application-id>
    rocky doctor
    rocky --version

### `rocky apps`

Lists discovered applications and manifest status.

The JSON mode is intended for scripts and AI agents.

### `rocky validate`

Checks an application without modifying it.

Current validation includes:

- Application directory exists
- `manifest.json` exists
- Manifest is valid JSON
- Required manifest fields exist
- Application ID matches its directory
- Command structure is valid
- Environment structure is valid
- `main.py` exists
- `main.py` passes in-memory Python syntax validation
- `README.md` exists

Validation must not create `__pycache__` files or otherwise modify an
application.

### `rocky doctor`

Checks the overall runtime environment.

Current checks include:

- Runtime root exists
- Manager package exists
- Runtime Python exists and is executable
- Application directory exists
- Rocky Python modules import successfully
- systemd service is active
- Button socket exists and is a Unix socket

A healthy system should report:

    Result: 0 failure(s), 0 warning(s)

---

## 10. Boot and Runtime Sequence

Normal startup:

    Linux boots
        |
        v
    systemd starts zero2w-manager.service
        |
        v
    Manager daemon initializes
        |
        +-- Display Manager starts
        +-- Application Manager starts
        +-- Button Server starts
        +-- Button Service starts
        +-- Plugin discovery runs
        |
        v
    Launcher is rendered
        |
        v
    Rocky waits for input

Application launch:

    User selects application
        |
        v
    Menu Service requests launch
        |
        v
    Application Manager resolves manifest
        |
        v
    Application process starts
        |
        v
    Button events route to application
        |
        v
    Application exits or Long Select is held
        |
        v
    Application Manager performs cleanup
        |
        v
    Launcher is restored

---

## 11. Failure and Recovery Rules

Rocky should recover to the launcher whenever possible.

Expected behavior:

- Invalid manifests are excluded or marked invalid.
- A failed application launch must not crash the manager daemon.
- A crashed foreground application must be detected.
- Stale application state must be recovered during startup.
- Graceful termination should be attempted before forced termination.
- Shared hardware resources must be released during shutdown.
- The launcher should return after application termination.
- Runtime errors should be written to the system journal.

Primary diagnostic commands:

    rocky doctor
    rocky apps --all
    rocky validate <application-id>

Primary service logs:

    sudo journalctl -u zero2w-manager.service -n 100 --no-pager -o cat

Live logs:

    sudo journalctl -u zero2w-manager.service -f -o cat

---

## 12. Development Rules

All human developers and AI agents working on Rocky should follow these rules.

1. Preserve the launcher as the system's safe recovery interface.
2. Do not allow applications to read physical button devices directly.
3. Do not allow applications to take permanent ownership of shared hardware.
4. Extend through applications or SDK interfaces before modifying the daemon.
5. Keep application manifests declarative and machine-readable.
6. Validate generated or modified applications with `rocky validate`.
7. Run `rocky doctor` after runtime-level changes.
8. Compile modified Python modules before restarting the service.
9. Restart the service only after validation succeeds.
10. Inspect the journal after every runtime restart.
11. Avoid commands that silently overwrite working runtime files.
12. Back up a file before performing a large structural modification.
13. Keep CLI commands non-interactive when practical so agents can use them.
14. Machine-readable CLI output should use JSON.
15. Do not change the universal Long Select Home behavior without an explicit
    architecture decision.
16. Update this document whenever component ownership or communication paths
    change.

---

## 13. Change Validation Workflow

Before modifying code:

    rocky doctor
    rocky apps --all

After modifying an application:

    rocky validate <application-id>

After modifying runtime Python:

    /opt/zero2w-manager/venv/bin/python3 -m py_compile <changed-files>

After restarting Rocky:

    sudo systemctl restart zero2w-manager.service
    sleep 2
    rocky doctor
    sudo journalctl -u zero2w-manager.service -n 60 --no-pager -o cat

A change is not complete until the relevant validation succeeds.

---

## 14. Hermes Integration Boundary

Hermes is planned as an AI-agent layer installed alongside Rocky.

Rocky remains responsible for:

- Device runtime
- Hardware ownership
- Application lifecycle
- Application manifests
- Validation
- Launcher behavior
- Button routing
- Display ownership

Hermes may be responsible for:

- Agent reasoning
- Code-generation assistance
- Project analysis
- Documentation maintenance
- Generating Rocky applications
- Running Rocky validation commands
- Proposing runtime changes
- Managing approved development workflows

Hermes must not:

- Directly control LRADC or shared GPIO
- Bypass the Application Manager
- Replace the runtime service without approval
- Edit production files without backup and validation
- Disable Long Select recovery
- Treat generated code as valid without running Rocky checks

Preferred Hermes interaction path:

    Hermes
       |
       v
    Rocky CLI and documented SDK
       |
       v
    Rocky applications
       |
       v
    Rocky Runtime services

Hermes should use stable CLI commands instead of parsing implementation details
whenever possible.

---

## 15. Future Architecture

Planned or possible future components:

- Hermes AI-agent integration
- Notification service
- Settings service
- Audio service
- Package manager
- Application install and removal commands
- Application packaging format
- Runtime updater
- ESP32-S3 companion processor
- Voice interface
- Additional sensors
- Remote management interface

Future components must preserve the existing ownership boundaries:

- Rocky owns hardware and lifecycle.
- Applications own application behavior.
- Agents operate through documented tools and interfaces.

---

## 16. Current Status

Implemented:

- E-paper launcher
- Display lifecycle management
- Plugin discovery
- Application lifecycle management
- Foreground process monitoring
- Universal button IPC
- Button-aware SDK
- Rocky application generator
- Application listing
- Application validation
- Runtime health diagnostics
- systemd runtime service

Installed test application:

- SDK Test

Temporary development application:

- Generator Test

Next milestones:

1. Remove the temporary Generator Test application.
2. Back up the current Rocky Runtime.
3. Create the Hermes bootstrap plan.
4. Verify Hermes compatibility with ARM64 and the installed operating system.
5. Install Hermes in an isolated environment.
6. Integrate Hermes through Rocky's CLI and SDK boundaries.

---

## 17. Source of Truth

This file is the concise architectural source of truth for Rocky Runtime.

Before making changes, contributors and agents should read:

    /opt/zero2w-manager/ROCKY_ARCHITECTURE.md

When live code and this document disagree, contributors must inspect the live
implementation and update this document as part of the same approved change.
