# Agent-Aware Terminal
## Technical Specification — Version 0.2

**Date:** 30 October 2026
**Target platform for v1:** Linux Desktop, primarily Ubuntu 24.04; Wayland and X11
**Preferred stack:** Flutter for the interactive visual interface; native Linux capabilities for the terminal session and environment integration
**Plan:** four sequential phases

---

## 1. Product Idea and Main Goal

**The main goal of Agent-Aware Terminal is to keep the user engaged in their own task throughout long coding-agent sessions.**

When an agent independently analyzes code, runs tools, or makes changes, a regular terminal often turns into a static screen with an unclear status. The user loses the sense of progress, switches to other activities, and later has to rebuild the task context.

We want to turn these intervals into a **useful, pleasant, and interactive continuation of work on the project**. To do this, the application combines two full layers:

1. **Terminal:** the user gets a familiar console environment with all the capabilities of a regular Linux terminal, including full support for terminal applications and AI CLIs.
2. **Interactive overlay:** an additional visual space on top of the terminal that can show agent status, animations, architecture elements, contextual project information, and other interaction formats.

**The point of the overlay is not just to fill the screen, but to keep the user's interest and connect execution pauses to the current task.** It evolves together with the application's awareness: from a standalone interface layer to a visual representation of the agent's actions and the project's structure.

### 1.1. Primary User Scenario

1. The user opens the application and starts working in the terminal in the usual way.
2. When the user wants to switch to visual mode, they summon the overlay with a hotkey. A smooth animated layer with its own interactive elements appears.
3. The user returns to the terminal when they need to continue working with the console.
4. As phases are implemented, the application starts to understand what is happening in the terminal session and recognizes AI agent activity.
5. While the agent works autonomously, the overlay can open automatically: it shows what is happening and offers project-related content.
6. When the agent finishes a stage or waits for human input, the user easily returns to the terminal and continues the current task.

### 1.2. Key UX Hypothesis

**Interactive visual accompaniment directly connected to the current task and project can reduce the user dropping out of context while waiting for a coding agent.**

In later prototypes we validate not only subjective interface appeal but also the return to work: how often the user switches to other applications, how quickly they re-orient after a step completes, and how they rate their sense of control over the process.

---

## 2. Overall Product Model

### 2.1. Terminal Mode

The application looks and functions like a regular terminal. It supports launching a familiar shell, CLI programs, process management, editors, remote sessions, and terminal interfaces.

**The target requirement is to preserve 100% of regular terminal capabilities for typical user workflows.** The actual compatibility of the chosen engine is confirmed by tests, including complex full-screen programs, focus management, resize, scrollback buffer, and terminal sequences.

### 2.2. Interactive Overlay Mode

The overlay is part of the same workspace: when activated, it smoothly appears on top of the terminal content. The terminal becomes a visual background; the user interacts with overlay elements and then returns to the console. Switching modes preserves the continuity of the current work session.

Proposed overlay composition:

- **Upper visual zone (~50% of the area):** animation, a graphical scene, project architecture elements, and agent activity visualization. The area share is configurable and can evolve.
- **Lower information zone:** contextual facts, hints, interactive cards, information about the agent's current action, and controls.
- **A small status block:** agent name, current stage/action, and a progress indicator in whatever form available events can justify.
- **Visibility controls:** hotkey, close button, behavior and animation settings.

Visual concept: a modern minimal shell and expressive **pixel-art / game-inspired** animations. The concrete art style is refined through prototypes; the graphical scene must be extensible.

### 2.3. Overlay Behavior as It Evolves

| Capability | Introduced in phase |
|---|---|
| Manual show/hide, animated layer, theming | 1 |
| Understanding terminal session data and preliminary agent state detection | 2 |
| Structured agent events, automatic display during work | 3 |
| Project architecture elements, project cards, graphical context representation | 4 |

---

## 3. Technical Architecture: Target Scheme

```text
+-------------------------------------------------------------+
| Agent-Aware Terminal — Linux Desktop                        |
|                                                             |
|  +-------------------------------------------------------+  |
|  | Interactive Overlay UI (Flutter)                      |  |
|  | Transitions / Visual scene / Status / Context cards   |  |
|  +--------------------------+----------------------------+  |
|                             | UI state + events             |
|  +--------------------------v----------------------------+  |
|  | App Controller                                        |  |
|  | OverlayState / TerminalState / AgentState / Project   |  |
|  +----------+---------------------+----------------------+  |
|             |                     |                         |
|  +----------v----------+ +--------v---------------------+  |
|  | Terminal Host       | | Context & Integration        |  |
|  | Native emulator     | | Session Observer             |  |
|  | PTY / shell / CLI   | | Agent Adapters               |  |
|  |                     | | Project Context Indexer      |  |
|  +----------+----------+ | Architecture Model           |  |
|             |            +------------------------------+  |
|        Bash / Zsh / SSH / Codex / Claude Code / ...         |
+-------------------------------------------------------------+
```

### 3.1. Terminal Layer

The terminal engine is chosen by the quality of its work with real Linux CLIs. Functional completeness and convenience matter more than a Dart-only implementation. Candidate for Linux: **GTK/VTE with PTY**; alternatives may be considered after technical validation.

At the start, an integration prototype of Flutter + a native terminal component is required: verify joint rendering, transparency, and interaction on Wayland/X11. The composition method is chosen based on this prototype's results. The UI structure must not depend on the details of a specific terminal engine.

### 3.2. Visual Layer

Flutter is responsible for screens, transitions, interactive components, system theming, and visualizations. A typed event and command interface is used between the UI and the native part.

### 3.3. Context and Integration Layer

Independent modules are added per phase:

- `SessionObserver` — information about the terminal session and the actions happening in it.
- `AgentAdapter` — adapting a specific coding agent's events to the common model.
- `AgentStateModel` — the state of the agent and the current stage.
- `ProjectContextIndexer` — analysis of the current project's structure.
- `ArchitectureModel` — components, connections, files, information sources.
- `ContextContentProvider` — facts and other content elements for the overlay.
- `LocalStorage` — settings, saved cards, and prepared project data.

These are **proposed module boundaries**, not a prescription to implement them all at once.

---

## 4. Phase 1 — A Full Terminal with an Interactive Overlay

### Goal

Deliver a working Linux Desktop application where the user performs familiar tasks in a terminal and, via a hotkey, opens a beautiful interactive visual layer over the current terminal content.

### Functionality

**1.1. Terminal session**

- Launch the user's shell with the familiar environment and working directory.
- Execute commands and interactive CLI applications.
- Text input/output, scrollback, selection and copying, color schemes, Unicode, resize.
- Work with full-screen TUI applications, `vim`/`nvim`, `tmux`, `htop`, `ssh`, and coding-agent CLIs.
- Continuous terminal session state across display-mode transitions.

**1.2. Overlay**

- In normal mode the visual layer is not visible.
- Default hotkey: `Ctrl+Shift+O` (configurable combination).
- Smooth show and hide: fade, slight scale/motion, visual dimming of the terminal.
- A close button is available; overlay content has its own interactive elements.
- The terminal session remains available after returning from visual mode.
- In demo state the overlay contains a beautiful animated composition and an example interactive card.

**1.3. Theming**

- Use the Linux system light/dark theme preference; where possible, accent color and contrast settings.
- React to theme changes while the application is running.
- Manual theme choice in settings.
- A set of visual tokens (palette, typography, radii, animation parameters) for later phases.

**1.4. Interface settings**

- Animation duration and character.
- Overlay show and hide behavior.
- Prepare the layout of upper and lower areas so they can later be filled with data.

### Technical check before UI implementation

First build a small prototype: a full terminal surface + a Flutter visual layer in one user window. Verify on the target Linux environment: compositing, transparency, focus changes, performance, full-screen TUIs, and window resizing. Based on the results, fix the concrete terminal host implementation.

### Phase 1 acceptance criteria

1. The application launches on Ubuntu Linux and provides practically full terminal work across a verified list of scenarios.
2. The user opens and closes the overlay via hotkey and button; the transition looks smooth.
3. The overlay is visually designed and interactive, not a static picture.
4. After returning to the terminal, the same application keeps working in the same terminal session.
5. The system light/dark theme is read and used in the design.

**Result:** a standalone demo product — a terminal with an expressive interactive layer.

---

## 5. Phase 2 — Understanding What Is Happening in the Terminal

### Goal

Teach the application to read the available terminal session context and build a preliminary model of what is happening: what is running, what is changing, and whether current activity looks like a coding agent at work.

### Functionality

**2.1. Session data sources**

- Terminal component events and child-process lifecycle.
- Working directory and active process information, when available.
- Available terminal text context and its changes.
- Shell integration for more precise information about commands and transitions between them, if the environment supports it.

**2.2. State interpretation**

- Detect the active terminal session and the current foreground process.
- Recognize the launch of a supported agent CLI.
- Preliminarily detect prolonged activity, waiting, and possible transitions to user input.
- Separate direct observations from the application's assumptions so the interface can show the degree of confidence.

**2.3. Diagnostics panel**

- Current process and working directory.
- Terminal session state and presumed agent state.
- Event timeline with source and time.
- A debug view for verifying recognition in real scenarios.

### Phase 2 acceptance criteria

1. The system detects the launch and termination of at least one agent CLI.
2. It displays the currently observed context without disrupting normal terminal work.
3. It shows the difference between known state and assumption.
4. It can work with terminal processes for which no specific agent adapter exists yet.

**Result:** the terminal starts to understand what is happening and is ready to pass this data to the visual layer.

---

## 6. Phase 3 — Coding Agent Integration and Automatic Visual Mode

### Goal

Connect the application to structured AI agent events, display task progress, and automatically open the visual layer at suitable moments of autonomous agent work.

### Functionality

**3.1. Agent adapters**

- Support a pluggable `AgentAdapter` interface.
- For the first implementation choose one agent, e.g. Codex; then add Claude Code and other CLIs as needed.
- Use available agent APIs, lifecycle events, hooks, and tool data depending on each agent's capabilities.
- Preserve a common event format regardless of the agent vendor.

**3.2. Current-work model**

Possible states: `starting`, `planning`, `working`, `tool_running`, `awaiting_user`, `completed`, `failed`, `unknown`.

An event contains, when the data is available:

```json
{
  "agent": "codex",
  "session_id": "local-session-42",
  "state": "tool_running",
  "activity": "Editing decoder module",
  "tool": "file_edit",
  "files": ["src/audio/decoder.rs"],
  "source": "agent_event",
  "timestamp": "2026-10-30T21:00:00Z"
}
```

This is an example of the **internal Agent-Aware Terminal contract**, not any specific agent's API format.

**3.3. Automatic overlay behavior**

- The application recognizes the start of autonomous agent work and can smoothly activate the overlay.
- The user controls auto-show behavior in settings.
- The close button allows returning to the terminal at any moment.
- On task completion or when human participation is needed, the interface rearranges itself so the required action is obvious.
- Provide resistance to frequent status changes so visual mode does not flicker.

**3.4. Activity display**

- Agent name and the task it is working on.
- Current stage, running tool, involved files.
- Step plan, when the agent provides one.
- A progress bar based on known stages; if a concrete percentage is undefined — an activity indicator.
- A timeline of key events, available in visual mode.
- Initial animated visualization of actions: e.g. an object moves between the conventional modules `Analyze`, `Edit`, `Test`.

### Phase 3 acceptance criteria

1. The user launches a supported coding agent inside the terminal.
2. The application receives its events, shows current activity, and reacts visually.
3. During long execution the overlay automatically appears according to user settings.
4. The user can close the overlay and continue interacting with the agent in the terminal.
5. Plan/status update correctly on editing, command runs, participation requests, and completion.

**Result:** the user sees a meaningful visual representation of the agent's work instead of an opaque `Working...`.

---

## 7. Phase 4 — Project Understanding and Visual Content Creation

### Goal

Teach the application to analyze the project the user is working on and turn the analysis results into structural documentation, a component diagram, and interactive overlay content.

### Functionality

**4.1. Project detection**

- Obtain the working directory from the terminal session.
- Find the Git repository root and related configuration files.
- Allow choosing or changing the active project manually.
- Support multi-module projects and prepare for monorepos.

**4.2. Structure analysis**

- Read the project tree, README, available documentation, and configurations.
- Extract files, modules, packages, services, and dependencies.
- Build an intermediate structured model of components and connections.
- Bind model elements to source files and documentation.
- Update the model when the project changes.

**4.3. Documentation generation**

- A short summary of the project's purpose.
- A list of the main subsystems and their responsibilities.
- A component interaction diagram.
- An explanation of a selected component at the user's request.
- Navigation from a description or graphical element to the related source file.

**4.4. Graphical primitives**

Prepare a library of visualization elements:

- `ComponentNode` — an architectural component;
- `Connection` — a dependency or data flow;
- `AgentMarker` — agent presence/activity in some area of the project;
- `ActivityAnimation` — processing, change, verification;
- `InfoCard` — a fact or explanation about the project;
- `ProgressElement` — a stage or status of the current task.

One architecture model must admit several representations: a simple diagram and a more expressive animated scene.

**4.5. Content and interaction**

- Show contextual project cards while the agent works.
- Cards can explain component purposes, connections between them, or business-logic specifics.
- Allow marking liked material and saving it for later viewing.
- Allow navigating from a card to the information source in the project.
- The prepared system of visual primitives and content blocks must allow expanding interaction scenarios in later versions.

### Phase 4 acceptance criteria

1. The application discovers the current project and builds its preliminary structural model.
2. An observable architecture diagram and short documentation are formed from the project.
3. Agent actions are linked to files and architectural components when the mapping is known.
4. The visual layer shows real project components and their connections, not only an abstract demo scene.
5. The user sees contextual cards, interacts with them, and can save interesting materials.

**Result:** the interactive overlay becomes a personalized visual representation of the project and the agent's work on it.

---

## 8. General Properties and Quality Criteria

### 8.1. Terminal compatibility

Verified with real user scenarios:

| Scenario | What we check |
|---|---|
| Shell / CLI | Bash, Zsh, history, environment variables, command execution |
| Editors / TUI | Vim, Neovim, less, htop, tmux, alt-screen |
| Remote | SSH, remote TUI behavior |
| Coding agents | Codex and other agent CLIs |
| Terminal UX | Resize, scrollback, ANSI colors, UTF-8, paste, selection |
| Overlay | Open/close during interactive and long-running commands |

### 8.2. Visualization quality

- Animation smoothness; the target guideline for a regular scene is 60 FPS on suitable hardware.
- Visual compliance with the system theme.
- Low overhead during normal terminal use.
- Readable overlay elements, coherent composition, aesthetically integral transitions.
- Evolution from abstract animation to displaying real actions and architecture.

### 8.3. User experience control

- Convenient manual switching of visual mode.
- Configurable automatic appearance in phase 3.
- Fast return to console interaction.
- Persistence of chosen appearance and visual behavior settings.
- Clear display of which information is known and which is approximately determined by the application.

### 8.4. Project data and integrations

- The source and freshness of architectural information are available through the data model.
- Settings for context access and analysis method can evolve together with integrations.
- Work with local repositories, with the possibility of later connecting additional project documentation sources.

---

## 9. Development and Demonstration Sequence

| Stage | Main demonstration | Readiness sign |
|---|---|---|
| **Spike 1** | Native terminal and Flutter layer in a Linux window | A confirmed composition method for Wayland/X11 |
| **Phase 1** | Regular terminal → hotkey → beautiful overlay → back | Compatible terminal session, interactive scene, system themes |
| **Phase 2** | An agent launches in the terminal → the panel shows recognized events | Observed context and a diagnostic state model |
| **Phase 3** | The agent works → the overlay appears and displays actions | A live adapter for one agent and controlled auto-show |
| **Phase 4** | The agent works with a file → the project component is highlighted, related content appears | Architecture model, diagram, cards, and saving interesting items |

Each phase ends with a working build, a demonstration of the user scenario, and a short list of technical decisions for the next stage.

---

## 10. Questions Resolved by Prototypes

1. **Terminal engine and Flutter:** which joint rendering method will give the best functional compatibility and visual freedom on Linux?
2. **Scene composition:** how best to distribute space between pixel graphics, agent status, and interactive content?
3. **State detection:** which terminal events are most useful before structured agent adapters are connected?
4. **Auto-show:** which intervals of autonomous agent work are long enough for visual mode to be pleasant and useful?
5. **Staying in context:** which kinds of project information actually help the user return to their task faster?

These are questions for research and tests, not pre-closed product decisions.

---

## 11. The Product's Key Success Criterion

**The user prefers to stay in Agent-Aware Terminal while the AI agent works, because they receive understandable, interesting, project-connected interactive accompaniment — while retaining the full familiar working power of the terminal.**

At the early research level, success is assessed through:

- the user's desire to use this terminal daily;
- understanding of what the agent is busy with;
- frequency of leaving the work context while waiting;
- speed of returning to substantive work after an agent step;
- usefulness and attractiveness of the visual content.

**Working title:** Agent-Aware Terminal
**Current scope:** phases 1–4, culminating in a terminal with an interactive contextual visual layer, agent understanding, and project structure awareness.
