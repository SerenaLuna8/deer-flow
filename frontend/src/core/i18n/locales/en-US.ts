import {
  CompassIcon,
  GraduationCapIcon,
  ImageIcon,
  MicroscopeIcon,
  PenLineIcon,
  ShapesIcon,
  SparklesIcon,
  VideoIcon,
} from "lucide-react";

import type { Translations } from "./types";

export const enUS: Translations = {
  // Locale meta
  locale: {
    localName: "English",
  },

  // Common
  common: {
    home: "Home",
    settings: "Settings",
    delete: "Delete",
    edit: "Edit",
    rename: "Rename",
    share: "Share",
    openInNewWindow: "Open in new window",
    close: "Close",
    more: "More",
    search: "Search",
    loadMore: "Load more",
    reload: "Reload",
    retry: "Retry",
    historyLoadFailed: "Conversation history could not be loaded safely.",
    download: "Download",
    file: "file",
    thinking: "Thinking",
    thinkingProcess: "Reasoning",
    thinkingInProgress: (seconds?: number) =>
      seconds === undefined ? "Thinking…" : `Thinking… (${seconds}s)`,
    thoughtFor: (seconds?: number) =>
      seconds === undefined
        ? "Reasoning"
        : seconds === 0
          ? "Thought (under 1 second)"
          : `Thought (${seconds} ${seconds === 1 ? "second" : "seconds"})`,
    artifacts: "Artifacts",
    public: "Public",
    custom: "Custom",
    notAvailableInDemoMode: "Not available in demo mode",
    loading: "Loading...",
    version: "Version",
    lastUpdated: "Last updated",
    code: "Code",
    preview: "Preview",
    cancel: "Cancel",
    save: "Save",
    install: "Install",
    create: "Create",
    import: "Import",
    export: "Export",
    exportAsMarkdown: "Export as Markdown",
    exportAsJSON: "Export as JSON",
    exportSuccess: "Conversation exported",
    regenerate: "Regenerate",
    editAndRerun: "Edit and rerun",
    updateAndRerun: "Update and rerun",
    editRerunWarning:
      "Rerunning restores conversation state only. Files, memory, and external actions are not undone.",
    branch: "Branch conversation",
    showArtifacts: "Show artifacts of this conversation",
    feedbackHelpful: "Helpful response",
    feedbackNotHelpful: "Not helpful response",
    feedbackSaveFailed: "Failed to save feedback",
  },

  runDuration: {
    working: "Working…",
    completedIn: (duration) => `Completed in ${duration}`,
    description:
      "Total task time, including model reasoning, tool calls, and waiting.",
    lessThanSecond: "<1s",
    hours: (value) => `${value}h`,
    minutes: (value) => `${value}m`,
    seconds: (value) => `${value}s`,
    separator: " ",
  },

  // Home
  home: {
    docs: "Docs",
  },

  // Welcome
  welcome: {
    greeting: "Hello, again!",
    description:
      "Welcome to 🦌 ActWeave, an open source super agent. With built-in and custom skills, ActWeave helps you search on the web, analyze data, and generate artifacts like slides, web pages and do almost anything.",

    createYourOwnSkill: "Create Your Own Skill",
    createYourOwnSkillDescription:
      "Create your own skill to release the power of ActWeave. With customized skills,\nActWeave can help you search on the web, analyze data, and generate\n artifacts like slides, web pages and do almost anything.",
  },

  // Clipboard
  clipboard: {
    copyToClipboard: "Copy to clipboard",
    copiedToClipboard: "Copied to clipboard",
    failedToCopyToClipboard: "Failed to copy to clipboard",
    linkCopied: "Link copied to clipboard",
  },

  // Citations
  citations: {
    sourcesSummary: (count) =>
      `Used ${count} ${count === 1 ? "source" : "sources"}`,
    citeCount: (count) => `${count} ${count === 1 ? "cite" : "cites"}`,
    copyReference: (title) => `Copy ${title} reference`,
    copiedReference: (title) => `Copied ${title} reference`,
  },

  // Workspace Changes
  workspaceChanges: {
    title: "Workspace changes",
    editedTitle: (count) => `Edited ${count} ${count === 1 ? "file" : "files"}`,
    badge: (count, additions, deletions) =>
      `${count} ${count === 1 ? "file" : "files"} changed +${additions} -${deletions}`,
    viewChanges: "View changes",
    created: "Created",
    modified: "Modified",
    deleted: "Deleted",
    openFile: "Open file",
    loading: "Loading workspace changes...",
    noChanges: "No workspace changes recorded.",
    diffUnavailable: "Diff unavailable",
    binaryUnavailable: "Binary file. Diff unavailable.",
    largeUnavailable: "Large file. Diff omitted.",
    sensitiveUnavailable: "Sensitive path. Content hidden.",
    truncatedUnavailable: "Diff omitted because the change set is too large.",
    truncatedSummary: "Some changes were truncated.",
  },

  // Input Box
  inputBox: {
    placeholder: "How can I assist you today?",
    createSkillPrompt:
      "We're going to build a new skill step by step with `skill-creator`. To start, what do you want this skill to do?",
    addAttachments: "Add attachments",
    inputPolish: "Polish input",
    inputPolishing: "Polishing input...",
    inputPolishNoChanges: "This input is already clear.",
    inputPolishFailed: "Failed to polish input.",
    inputPolishUndo: "Undo polish",
    inputPolishCancel: "Cancel polishing",
    voiceInputStartLabel: "Dictate with voice",
    voiceInputStopLabel: "Stop voice input",
    voiceInputStart:
      "Dictate with voice. ActWeave receives only transcribed text; audio is handled by your browser or system speech service.",
    voiceInputListening: "Listening... Click to stop voice input.",
    voiceInputUnsupported:
      "Voice input is not supported in this browser. Try Chrome or Edge.",
    voiceInputPermissionDenied:
      "Microphone access was denied. Allow microphone access and try again.",
    voiceInputMicrophoneUnavailable:
      "No microphone was detected. Check your device input and try again.",
    voiceInputUnsupportedLanguage:
      "Voice input does not support the current language in this browser.",
    voiceInputNetworkError:
      "Voice input could not reach the browser speech service.",
    voiceInputNoSpeech: "No speech was detected. Please try again.",
    voiceInputFailed: "Voice input failed. Please try again.",
    model: "Model",
    agentModelLocked: "This Agent is locked to this model",
    mode: "Mode",
    flashMode: "Flash",
    flashModeDescription:
      "Disable extended thinking and use no reasoning effort for simple tasks",
    reasoningMode: "Reasoning",
    reasoningModeDescription:
      "Enable extended thinking with low reasoning effort",
    proMode: "Pro",
    proModeDescription:
      "Enable extended thinking with medium reasoning effort to balance quality and speed",
    ultraMode: "Ultra",
    ultraModeDescription:
      "Enable extended thinking with high reasoning effort for complex tasks",
    searchModels: "Search models...",
    surpriseMe: "Surprise",
    surpriseMePrompt: "Surprise me",
    followupLoading: "Generating follow-up questions...",
    followupConfirmTitle: "Send suggestion?",
    followupConfirmDescription:
      "You already have text in the input. Choose how to send it.",
    followupConfirmAppend: "Append & send",
    followupConfirmReplace: "Replace & send",
    suggestionPlaceholderRequired:
      "Replace the suggestion placeholder before sending.",
    goalCommandDescription: "Set, show, or clear an active goal",
    compactCommandDescription:
      "Compact earlier context while keeping the full chat visible",
    dreamCommandDescription: "Compact this chat, then organize pending Memory",
    dreamLogCommandDescription: "Open Memory history, optionally at a version",
    dreamRestoreCommandDescription: "Confirm and restore a Memory version",
    goalLabel: "Goal",
    goalContinuing: "Continuing {count}/{max}",
    goalContinuationTooltip:
      "Auto-continued {count}/{max} times toward the goal; stops at the limit.",
    goalSet: "Goal set.",
    goalCleared: "Goal cleared.",
    goalNone: "No active goal.",
    goalActive: "Active goal: {goal}",
    goalFailed: "Goal command failed.",
    compactSuccess:
      "Earlier context compacted. The full chat remains visible; future model calls will use the summary and recent messages.",
    compactSkipped: "The current context does not need compaction yet.",
    compactFailed: "Context compaction failed.",
    dreamQueued: "Started organizing {count} Memory items.",
    dreamAlreadyRunning: "Memory organization is already running.",
    dreamNothingPending: "There is no Memory waiting to be organized.",
    dreamInvalidArguments:
      "/Dream does not accept arguments. Send /Dream by itself.",
    dreamLogInvalidArguments:
      "Use /dream-log or /dream-log followed by one positive version number.",
    dreamRestoreInvalidArguments:
      "Use /dream-restore followed by one positive version number.",
    dreamAttachmentsUnsupported: "Memory commands cannot include attachments.",
    dreamFailed: "Failed to organize Memory.",
    dreamRequiresThread: "/Dream requires an existing chat.",
    dreamPreparationStarted: "Dream preparation started in the background.",
    dreamPreparationQueued: "Dream preparation is queued.",
    dreamPreparationRunning: "Archiving earlier chat turns for Dream…",
    dreamPreparationVerifying:
      "Verifying the archived chat and starting Dream…",
    dreamPreparationCompleted: "Dream preparation completed.",
    dreamPreparationCancelled: "Dream preparation was cancelled.",
    dreamPreparationFailed: "Dream preparation failed.",
    dreamPreparationPasses: "({count} archive passes)",
    dreamPreparationCancel: "Cancel",
    dreamPreparationCancelRequested: "Cancellation requested",
    dreamRouteUnavailable: "Memory is not available from this chat.",
    dreamRestoreSuccess: "Restored Memory as new version {version}.",
    dreamRestoreFailed: "Failed to restore this Memory version.",
    dreamRestoreConfirmTitle: "Restore Memory version {version}?",
    dreamRestoreConfirmDescription:
      "This writes the selected historical content as a new current version. Later history is preserved.",
    dreamRestoreConfirmAction: "Restore version",
    suggestions: [
      {
        suggestion: "Write",
        prompt: "Write a blog post about the latest trends on [topic]",
        icon: PenLineIcon,
      },
      {
        suggestion: "Research",
        prompt:
          "Conduct a deep dive research on [topic], and summarize the findings.",
        icon: MicroscopeIcon,
      },
      {
        suggestion: "Collect",
        prompt: "Collect data from [source] and create a report.",
        icon: ShapesIcon,
      },
      {
        suggestion: "Learn",
        prompt: "Learn about [topic] and create a tutorial.",
        icon: GraduationCapIcon,
      },
    ],
    suggestionsCreate: [
      {
        suggestion: "Webpage",
        prompt: "Create a webpage about [topic]",
        icon: CompassIcon,
      },
      {
        suggestion: "Image",
        prompt: "Create an image about [topic]",
        icon: ImageIcon,
      },
      {
        suggestion: "Video",
        prompt: "Create a video about [topic]",
        icon: VideoIcon,
      },
      {
        type: "separator",
      },
      {
        suggestion: "Skill",
        prompt:
          "We're going to build a new skill step by step with `skill-creator`. To start, what do you want this skill to do?",
        icon: SparklesIcon,
      },
    ],
    pleaseWaitStreaming: "Please wait for the current response to finish.",
  },

  // Sidebar
  sidebar: {
    newChat: "New chat",
    chats: "Chats",
    channels: "Channels",
    recentChats: "Recent chats",
    demoChats: "Demo chats",
    agents: "Agents",
    scheduledTasks: "Scheduled tasks",
    agentsDisabledTooltip: "Feature not enabled",
  },

  project: {
    audit: "Audit",
    automations: "Automations",
    agents: "Agent",
    skills: "Skill",
    mcp: "MCP",
    memory: "Memory",
    usage: "Usage",
    governance: {
      retry: "Retry",
      tokenSeries: {
        title: "Token usage trend",
        description:
          "Model token consumption across all project members for the latest 24 hourly buckets.",
        loading: "Loading token usage",
        unavailableTitle: "Token usage is unavailable",
        unavailableDescription:
          "Project token usage could not be read safely. Try again later.",
        emptyTitle: "No token usage in the latest 24 hours",
        emptyDescription:
          "The trend will appear after a job settles with reported token usage.",
        window: "Latest 24 hours",
        settlementNote: "Grouped into hourly buckets by job settlement time",
        interactionHint: "Hover or focus any hour to inspect its details",
        chartLabel: "Project token usage line chart for the latest 24 hours",
        tableCaption: "Project token usage details for the latest 24 hours",
        bucket: "Hour",
      },
      usage: {
        title: "Project usage",
        description: "Review effective limits and current project consumption.",
        settingsDescription:
          "System admins can tighten this project's quota ceilings here. Live occupancy and effective limits remain on the project overview.",
        loading: "Loading usage",
        unavailableTitle: "Usage is unavailable",
        unavailableDescription:
          "The project usage service could not be read safely.",
        thresholdReached: "80% threshold reached",
        used: "Used",
        reserved: "Reserved",
        limit: "Limit",
        tightenTitle: "Tighten project limits",
        updateError: "Limits were not updated. Check the values and retry.",
        updateConflict:
          "Another administrator updated these limits. Refresh before saving again.",
        updateUnavailable:
          "The quota service is temporarily unavailable. Try again later.",
        platformLimitRule:
          "Project limits must be equal to or stricter than the platform limits. Check the values.",
        platformLimitExceeded: (dimension, limit) =>
          `${dimension} cannot exceed the platform limit of ${limit}. Project limits must be equal to or stricter than platform limits.`,
        saving: "Saving…",
        save: "Save limits",
        dimensions: {
          members: "Members",
          storage_bytes: "Storage bytes",
          concurrent_runs: "Concurrent runs",
          mcp_calls_daily: "Daily MCP calls",
        },
      },
      audit: {
        title: "Project audit",
        description: "Review privacy-safe project governance history.",
        loading: "Loading audit",
        unavailableTitle: "Audit is unavailable",
        unavailableDescription: "Audit history could not be read safely.",
        emptyTitle: "No audit events",
        emptyDescription: "This project has no recorded governance events yet.",
        olderEvents: "Older events",
        previousPage: "Previous",
        nextPage: "Next",
        page: (page) => `Page ${page}`,
        pageSize: "Per page",
        pageSizeOption: (pageSize) => `${pageSize}`,
        itemsOnPage: (count) => `${count} on this page`,
        columns: {
          time: "Time",
          action: "Action",
          outcome: "Outcome",
          actor: "Actor",
          target: "Target",
          details: "Details",
          error: "Error",
        },
      },
    },
  },

  projectMemory: {
    title: "Memory",
    description:
      "A private long-term document gradually organized from your conversations.",
    currentTab: "Current memory",
    archiveTab: "Archive",
    documentFileName: "MEMORY.md",
    mediaType: "text/markdown",
    version: (value) => `Version ${value}`,
    updated: "Last updated",
    neverUpdated: "Not organized yet",
    viewMode: "Document display mode",
    source: "Source",
    preview: "Preview",
    viewChanges: "View changes",
    versionsTitle: "Version history",
    versionsDescription:
      "Inspect the real document version created by every organization or restore.",
    versionsFailed: "Version history could not be loaded",
    noVersions: "No historical versions yet.",
    previous: "Previous",
    next: "Next",
    reviewTitle: "Latest version needs review",
    reviewDescription:
      "This organization removed a large share of existing content. Inspect the real diff to confirm that no important memory was lost.",
    reviewAction: "Review this version",
    emptyTitle: "No long-term memory yet",
    emptyDescription:
      "As you keep talking, ActWeave records pending notes and Dream organizes them into this document.",
    pending: "Pending",
    pendingUnit: (value) => `${value} ${value === 1 ? "item" : "items"}`,
    dream: "Organize now",
    dreaming: "Organizing",
    dreamRunning: "An organization job is already running",
    dreamUnavailable: "You do not have permission to run an organization job.",
    dreamQueuedBudget:
      "Started compressing the Memory document into the current injection budget.",
    dreamQueuedItems: (value) =>
      `Started organizing ${value} Memory ${value === 1 ? "item" : "items"}.`,
    dreamAlreadyRunning: "A Memory organization job is already running.",
    dreamNothingPending: "There is no Memory waiting to be organized.",
    dreamFailed: "Memory organization failed.",
    restoreSucceeded: (version) => `Restored as new version ${version}.`,
    restoreFailed: "Memory restore failed.",
    autoDream: "Automatic Dream",
    manualDream: "Manual Dream",
    restoreTrigger: "Version restore",
    budgetRewrite: "Budget compression",
    handled: (value) => `Processed ${value} ${value === 1 ? "item" : "items"}`,
    changed: "Document changed",
    unchanged: "No content change",
    needsReview: "Review suggested",
    overBudgetTitle: "Memory document exceeds the injection budget",
    overBudgetDescription:
      "New conversations temporarily run without this memory document until it is compressed into budget.",
    injectionInactiveTitle:
      "Memory is not currently injected into new conversations",
    injectionPlatformDisabledDescription:
      "Long-term Memory is currently disabled by the platform. This reflects current settings only and does not prove what any existing conversation injected.",
    injectionAccountDisabledDescription:
      "Long-term Memory is currently disabled for your account. This reflects current settings only and does not prove what any existing conversation injected.",
    compressNow: "Compress document now",
    detailsTitle: (value) => `Version ${value}`,
    detailsDescription:
      "View the saved document and its real diff from the preceding version.",
    diffTitle: "Document change",
    diffTruncatedTitle: "Document change is truncated",
    diffTruncatedDescription:
      "Up to the first 64,000 characters are shown on complete line boundaries. Use the full saved document below for a complete review.",
    documentTitle: "Document at this version",
    noDiff: "This organization run did not change the document.",
    restore: "Restore this version",
    restoring: "Restoring",
    restoreTitle: (value) => `Restore version ${value}?`,
    restoreDescription:
      "Restore writes this historical content as a new current version. Later history remains available.",
    cancel: "Cancel",
    confirmRestore: "Restore",
    loadFailed: "Memory could not be loaded",
    detailFailed: "Version details could not be loaded",
    retry: "Retry",
    archiveDescription:
      "Original memory items already organized into the document are archived here and searchable by text and tag.",
    searchPlaceholder: "Search archived memory…",
    search: "Search",
    archiveFailed: "Archive could not be loaded",
    archiveEmpty: "No archived memory items yet.",
    archiveNoMatch: "No archived memory matched this search.",
    loadMore: "Load more",
    loadingMore: "Loading",
    originSnip: "Auto summary",
    originRemember: "Remembered",
    pendingTitle: "Pending items",
    pendingDescription:
      "These items are recorded and will be organized into the memory document by the next Dream.",
    pendingFailed: "Pending items could not be loaded",
    tags: {
      permanent: "Permanent",
      durable: "Durable",
      ephemeral: "Ephemeral",
      correction: "Correction",
    },
  },

  adminOperations: {
    shellTitle: "Platform administration",
    shellDescription: "Operations, assets, and settings",
    signOut: "Sign out",
    retry: "Retry",
    gatewayUnavailable: {
      title: "Platform administration is unavailable",
      description:
        "The Gateway could not be reached, so your administrator session could not be verified. No administration data was loaded.",
      reload: "Reload page",
    },
    ui: {
      navigationGroups: {
        operations: "Operations and governance",
        governance: "Platform configuration",
      },
      skipToContent: "Skip to main content",
      close: "Close",
      backToWorkspace: "Back to project workspace",
      expandNavigation: "Expand navigation",
      collapseNavigation: "Collapse navigation",
      previousPage: "Previous",
      nextPage: "Next",
      page: (page) => `Page ${page}`,
      pageSize: "Per page",
      pageSizeOption: (pageSize) => `${pageSize}`,
      itemsOnPage: (count) => `${count} on this page`,
      copy: "Copy",
      copied: "Copied",
      platformHealthy: "Systems operational",
      publicErrorCode: "Public error code",
      eventId: "Event ID",
      jobId: "Job ID",
      clearFilters: "Clear filters",
    },
    navigation: {
      label: "Platform administration navigation",
      overview: "Overview",
      projects: "Projects",
      jobs: "Jobs",
      audit: "Logs",
      assets: "Assets",
      systemSettings: "System settings",
      settings: "Model settings",
    },
    overview: {
      title: "Operations overview",
      loading: "Loading platform operations",
      unavailableTitle: "Operations data is unavailable",
      unavailableDescription: "Platform operations could not be loaded.",
      readiness: {
        title: "Readiness",
        workerCount: "Worker processes",
        workerCapacity: "Worker capacity",
        oldestHeartbeat: "Oldest Worker heartbeat",
        schedulerOwnership: "Scheduler ownership",
        secondsAgo: "{seconds}s ago",
        notReported: "Not reported",
        states: {
          ready: "Ready",
          degraded: "Degraded",
          closed: "Closed",
          unavailable: "Unavailable",
          disabled: "Disabled",
          polling: "Polling",
          owned: "Owned",
          unowned: "Unowned",
          ownership_lost: "Ownership lost",
          unknown: "Unknown",
        },
        components: {
          database: "Database",
          schema: "Schema",
          worker_fleet: "Worker fleet",
          scheduler: "Scheduler",
          stream: "Stream",
          quota: "Quota",
          audit: "Logs",
        },
      },
      channels: {
        title: "Channel providers",
        emptyTitle: "No provider health reports",
        empty: "No channel providers are configured or reporting health.",
      },
      counts: {
        projects: "Projects",
        suspendedProjects: "Suspended projects",
        queuedJobs: "Queued jobs",
        runningJobs: "Running jobs",
        deadJobs: "Dead jobs",
      },
      usage: {
        title: "Aggregate usage",
        members: "Members",
        storage_bytes: "Storage bytes",
        concurrent_runs: "Concurrent runs",
        mcp_calls_daily: "Daily MCP calls",
        used: "Used",
        reserved: "Reserved",
      },
    },
    projects: {
      title: "Projects",
      loading: "Loading projects",
      unavailableTitle: "Operations data is unavailable",
      unavailableDescription: "Project operations could not be loaded.",
      emptyTitle: "No projects found",
      emptyDescription: "No projects match the current view.",
      older: "Older projects",
      suspended: "Suspended",
      active: "Active",
      pendingDeletion: "Pending deletion",
      details: "Governance details",
      fields: {
        projectId: "Project ID",
        slug: "Project slug",
        createdAt: "Created",
        updatedAt: "Updated",
        deletionAt: "Deletion effective",
      },
      filters: {
        query: "Search",
        queryPlaceholder: "Search by project name or slug",
        status: "Lifecycle status",
        suspension: "Platform suspension",
        notSuspended: "Not suspended",
        all: "All",
        apply: "Apply filters",
        clear: "Clear",
        invalid: "Search text must contain 1 to 120 characters.",
      },
      actions: {
        governAssets: "Govern shared assets",
        suspend: "Platform suspend",
        resume: "Resume",
        pending: "Updating…",
        error:
          "The platform suspension state could not be updated. Refresh and retry.",
        confirmSuspendTitle: "Suspend this project?",
        confirmSuspendDescription:
          "Platform suspension immediately freezes member private work, blocks new runs, and revokes active run authority without changing lifecycle state or deleting data.",
        confirmResumeTitle: "Resume this project?",
        confirmResumeDescription:
          "Members regain private-work access. Automations that were paused remain paused.",
        cancel: "Cancel",
        confirm: "Confirm",
      },
    },
    jobs: {
      title: "Jobs",
      loading: "Loading jobs",
      unavailableTitle: "Operations data is unavailable",
      unavailableDescription: "Job operations could not be loaded.",
      emptyTitle: "No jobs found",
      emptyDescription: "No jobs match the current view.",
      older: "Older jobs",
      requeue: "Requeue safe job",
      requeueing: "Requeueing",
      requeueError: "The safe requeue could not be completed.",
      copyProjectId: "Copy project UUID",
      projectIdCopied: "Project UUID copied",
      statuses: {
        queued: "Queued",
        leased: "Leased",
        running: "Running",
        retry_wait: "Waiting to retry",
        succeeded: "Succeeded",
        failed: "Failed",
        cancelled: "Cancelled",
        dead: "Dead",
      },
      types: {
        private_run: "Project chat run",
        automation_run: "Automation run",
        retention_purge: "Retention purge",
        mcp_discovery: "MCP tool discovery",
        memory_dream: "Memory Dream",
        memory_dream_prepare: "Memory Dream preparation",
        memory_seal: "Memory idle seal",
      },
      retrySafetyLabel: "Safe retry",
      errorLabel: "Error",
      retrySafety: {
        safe: "Safe to retry",
        unsafe: "Unsafe to retry",
        unknown: "Unknown",
      },
      filters: {
        label: "Filter jobs",
        project: "Project",
        allProjects: "All projects",
        status: "Status",
        type: "Job type",
        allStatuses: "All statuses",
        allTypes: "All job types",
        apply: "Search",
        clear: "Reset",
      },
    },
    audit: {
      title: "Logs",
      loading: "Loading logs",
      unavailableTitle: "Operations data is unavailable",
      unavailableDescription: "Logs could not be loaded.",
      emptyTitle: "No logs found",
      emptyDescription: "No logs match the current view.",
      older: "Older logs",
      columns: {
        time: "Time",
        action: "Action",
        outcome: "Outcome",
        actor: "Actor",
        target: "Target",
        project: "Project",
        error: "Error",
      },
      filters: {
        label: "Filter logs",
        project: "Project",
        allProjects: "All projects",
        platformOnly: "Admin operations",
        apply: "Search",
        clear: "Reset",
      },
    },
  },

  adminModelSettings: {
    header: {
      eyebrow: "System settings",
      title: "Model settings",
      create: "Create model",
    },
    overview: {
      label: "Model catalog overview",
      configured: "Configured models",
      active: "Active models",
      defaultModel: "Default model",
      notSet: "Not set",
      revision: "Catalog revision",
    },
    states: {
      loading: "Loading model catalog",
      unavailableTitle: "Model settings are unavailable",
      unavailableDescription:
        "No unverified data is shown. Check Gateway and database health, then retry.",
      retry: "Retry",
      emptyTitle: "No models are available yet",
      emptyDescription:
        "Create your first model, enable it, and set it as the default.",
      catalogLabel: "Model catalog",
    },
    card: {
      defaultModel: "Default model",
      active: "Enabled",
      suspended: "Suspended",
      updatedAt: (formattedDate) => `Updated ${formattedDate}`,
      updatedAtColumn: "Updated",
      providerModel: "Model ID",
      credential: "Credential",
      environmentKey: "Environment variable",
      status: "Status",
      version: "Version",
      versionMeta: (versionNumber, revision) =>
        `Config v${versionNumber} · revision ${revision}`,
      capabilities: "Model capabilities",
      noCapabilities: "None",
      thinking: "Thinking",
      reasoningEffort: "Reasoning effort",
      vision: "Vision input",
      edit: "Edit",
      pause: "Suspend",
      enable: "Enable",
      currentDefault: "Current default",
      setDefault: "Set as default",
      actions: "Actions",
      actionFor: (action, name) => `${action}: ${name}`,
      defaultCannotPause: "The default model cannot be suspended",
      credentialUnbound: "No Credential bound",
      credentialUnavailable: "Credential bound (currently unavailable)",
      credentialHistorical: "Historical version",
    },
    adapters: {
      patchedOpenAI: "OpenAI enhanced compatibility",
      patchedDeepSeek: "DeepSeek enhanced compatibility",
      patchedMiMo: "MiMo enhanced compatibility",
      patchedMiniMax: "MiniMax enhanced compatibility",
      patchedStepFun: "StepFun enhanced compatibility",
    },
    editor: {
      editTitle: "Edit model",
      createTitle: "Create model",
      description:
        "Provider parameters are saved as model configuration. Credential references point to existing versions; secret values are never entered here.",
      basicInformation: "Basic information",
      basicDescription: "Define the Provider mapping and catalog details.",
      displayName: "Display name",
      displayNamePlaceholder: "Analysis Pro",
      providerAdapter: "Provider Adapter",
      retiredProviderAdapter: "No longer supported",
      retiredProviderAdapterHint:
        "Select a supported Provider Adapter before saving or testing the connection.",
      providerModel: "Model ID",
      status: "Status",
      active: "Enable",
      suspended: "Suspend",
      capabilities: "Model capabilities",
      capabilitiesAndRuntime: "Capabilities and runtime parameters",
      supportsThinking: "Supports thinking",
      supportsReasoningEffort: "Supports reasoning effort",
      supportsVision: "Supports vision input",
      commonProviderSettings: "Runtime parameters",
      baseUrl: "Base URL",
      baseUrlHint:
        "The Provider API base address. Do not include a model-specific path.",
      temperature: "Temperature",
      maxTokens: "Maximum Tokens",
      requestTimeout: "Request timeout (seconds)",
      maxRetries: "Maximum retries",
      credentialBinding: "Credential binding",
      systemCredential: "System Credential",
      credentialsUnavailableHint:
        "Credential metadata is unavailable, so the binding cannot be changed.",
      credentialSelectionHint:
        "Saving binds the selected Credential's current version without reading its value.",
      selectCredential: "Select a Credential",
      providerDoesNotUseCredential: "This Provider does not use a Credential",
      environmentKey: "Injected environment variable",
      environmentKeyHint:
        "Only the variable name is saved, for example OPENAI_API_KEY.",
      testConnection: "Test connection",
      testingConnection: "Testing…",
      testConnectionDescription:
        "Sends a minimal request with the current form configuration and selected Credential. It does not save the model or return a secret to the browser.",
      connectionSucceeded: "Connection succeeded",
      connectionFailed:
        "Connection failed. Check the Provider configuration and Credential, then try again.",
      advancedJson: "Advanced JSON",
      advancedJsonHint:
        "Only allowlisted parameters and fixed structures are supported, such as reasoning_effort, extra_body.reasoning.effort, and the thinking toggle. Unknown fields and arbitrary strings are rejected.",
      cancel: "Cancel",
      saving: "Saving…",
      saveChanges: "Save changes",
      createModel: "Create model",
    },
    validation: {
      invalidNumber: (label) => `${label} has an invalid format`,
      temperature: "Temperature",
      maxTokens: "Maximum Tokens",
      requestTimeout: "Request timeout",
      maxRetries: "Retry count",
      advancedJsonInvalid: "Advanced JSON is invalid",
      advancedJsonObject: "Advanced JSON must be an object",
      advancedJsonUnsafe:
        "Advanced JSON may contain only supported safe fields with exact value types",
      invalidForm:
        "Check required fields, display names, model IDs, and Credential binding",
      invalidConfiguration: "The model configuration is invalid",
    },
    actionErrors: {
      authRequired: "Your session has expired. Sign in again.",
      conflict:
        "Another administrator changed this model. Refresh and try again.",
      invalid: "The model configuration failed server validation.",
      generic: "The action was not completed. Refresh and try again.",
    },
    success: {
      updated: (name) => `Updated model “${name}”.`,
      created: (name) => `Created model “${name}”.`,
      enabled: (name) => `Enabled model “${name}”.`,
      suspended: (name) => `Suspended model “${name}”.`,
      defaultSet: (name) => `Set “${name}” as the default model.`,
    },
  },

  adminSystemSettings: {
    header: {
      eyebrow: "Platform configuration",
      title: "System settings",
      refresh: "Refresh",
      refreshing: "Refreshing",
    },
    states: {
      loading: "Loading system settings",
      unavailableTitle: "System settings are unavailable",
      unavailableDescription:
        "The current system settings could not be read safely.",
      retry: "Retry",
    },
    sections: {
      auth: {
        title: "Accounts and access",
        description:
          "Control new local-account registration requests without changing administrator setup or OIDC.",
      },
      automations: {
        title: "Automation scheduling",
        description:
          "Control Scheduler polling, the global concurrent Automation cap, and the minimum one-time delay. Manual triggers remain available when polling is off.",
      },
      quotas: {
        title: "Default quotas",
        description:
          "Set project quota defaults and the shared warning threshold.",
      },
      agentRuntime: {
        title: "Agent runtime policy",
        description:
          "Control budgets, context, Memory, tool results, and safeguards for new Runs. Active Runs are not changed.",
      },
      memoryDocument: {
        title: "Memory document sections",
        description:
          "Define the section template for newly created Memory documents. Existing documents keep the structure frozen when they were created.",
      },
    },
    groups: {
      runLimits: "Run budgets and limits",
      assistantExperience: "Assistant experience",
      summarization: "Context summarization",
      memory: "Project Memory",
      tools: "Tool discovery and output",
      safeguards: "Loop and file safety",
    },
    fields: {
      allowRegistration: "Allow visitors to register local accounts",
      defaultMemberLimit: "Default member limit",
      defaultStorageLimit: "Default storage byte limit",
      defaultConcurrentRuns: "Default concurrent Run limit",
      defaultDailyMcpCalls: "Default daily MCP call limit",
      quotaWarningThreshold: "Quota warning threshold",
      defaultModel: "Use the system default model",
      unavailableModel: "The referenced model is no longer available",
      addRow: "Add item",
      removeRow: "Remove",
      listHint:
        "Separate values with commas. Empty and duplicate values are removed before save.",
      memoryDocumentSections: "Section titles",
      memoryDocumentSectionsHint:
        "Enter 2–8 plain titles in document order. Do not include # or Dream history markers; the system creates the Markdown headings. Each title is limited to 80 characters and must be unique.",
      memoryDocumentSectionInput: (position) =>
        `Memory document section ${position}`,
      addMemoryDocumentSection: "Add section",
      removeMemoryDocumentSection: (position) => `Remove section ${position}`,
      moveMemoryDocumentSectionUp: (position) => `Move section ${position} up`,
      moveMemoryDocumentSectionDown: (position) =>
        `Move section ${position} down`,
    },
    common: {
      save: "Save changes",
      saving: "Saving",
      reset: "Discard changes",
      revision: (revision) => `Stored revision r${revision}`,
      effectiveRevision: (revision) => `Effective revision r${revision}`,
      updatedAt: (value) => `Updated ${value}`,
      storedRevision: (revision) => `Stored as r${revision}`,
      pendingRoles: (roles) => `Waiting for: ${roles}`,
      noPendingRoles: "No process refresh is pending",
    },
    effects: {
      newRequests: "Effective for later requests",
      newRuns: "Applies to new Runs",
      newRequestsAndRuns: "Applies to new requests and Runs",
      newMemoryDocuments: "New Memory documents only",
      nextAuthoritativeCheck: "Applies at the next quota check",
      restartRequired: "Applies after services restart",
    },
    feedback: {
      saved:
        "Settings were saved. The activation state below is server-confirmed.",
      registrationConfirmation:
        "This changes the visitor registration entry point. Save the accounts and access settings?",
      conflict:
        "The server revision changed. Your local edits are preserved; refresh, compare, and retry.",
      invalid: "A value is outside the safe range. Review this group.",
      inactiveModel:
        "Every model field must use a current active model or the system default.",
      authRequired: "Your administrator session expired. Sign in again.",
      generic: "Save failed. Your local changes are still preserved.",
    },
  },

  adminAssets: {
    navigation: {
      platformLabel: "Platform asset navigation",
      projectLabel: "Project asset governance navigation",
      agent: "Agent",
      skill: "Skill",
      mcp: "MCP",
      credential: "Credential",
      quota: "Quota",
    },
    shell: {
      platformAria: "Platform asset management",
      systemCatalog: "System asset catalog",
      adminScope: "Platform administrator scope",
      projectAria: "Project shared-asset governance",
      backToProjects: "Back to projects",
      projectGovernance: "Project shared-asset governance",
      projectId: "Project UUID",
    },
    common: {
      assetVersion: "Asset revision",
      versionId: "Version UUID",
      mcpConfigurationId: "Configuration UUID (internal revision)",
      currentPublishedVersion: "Current published version",
      currentPublishedMcpConfiguration: "Current published configuration",
      updatedAt: "Updated",
      versionHistory: "Version history",
      mcpConfigurationHistory: "Configuration history",
      versionCount: (count) =>
        `${count} ${count === 1 ? "version" : "versions"}`,
      mcpConfigurationCount: (count) =>
        `${count} ${count === 1 ? "configuration" : "configurations"}`,
      credentialMetadata: "Credential metadata",
      details: "Details",
      dangerZone: "Danger zone",
      type: "Type",
      selectCredentialType: "Select a Credential type",
      credentialTypes: {
        modelApiKey: "LLM key",
        apiKey: "API key",
        token: "Access token",
        mcpAuth: "MCP authentication",
        skillAuth: "Skill authentication",
        oauth: "OAuth authorization",
        database: "Database Credential",
      },
      transportTypes: {
        stdio: "Standard input/output (stdio)",
        sse: "Server-sent events (SSE)",
        http: "HTTP",
      },
      credentialPayloadGroups: {
        env: "Environment variables (env)",
        headers: "Request headers (headers)",
        query: "Query parameters (query)",
        oauth: "OAuth (oauth)",
      },
      metadataVersion: "Metadata revision",
      replaceCredential: "Replace Credential",
      migrateReferences: "Use new Credential version",
      revokeCredential: "Revoke Credential",
      delete: "Delete",
      createCredential: "Create Credential",
      createProjectCredential: "Create project Credential",
      retry: "Retry",
      retrying: "Retrying…",
      createVersion: "Create version",
      creatingVersion: "Creating…",
      reload: "Reload",
      systemProvided: "System provided",
      projectOwned: "Project owned",
      active: "Active",
      revoked: "Revoked",
      loading: "Loading",
      migrationSuccess:
        "Existing consumers now use the current Credential version.",
      credentialMigrationChecking:
        "Checking which consumers still use an older Credential version…",
      credentialMigrationUnavailable:
        "Credential version usage details are temporarily unavailable.",
      credentialMigrationComplete:
        "All consumers already use the current Credential version.",
      pendingMigrationNotice: (
        total,
        mcpGrantCount,
        skillBindingCount,
        systemModelCount,
      ) =>
        `${total} ${total === 1 ? "consumer still uses" : "consumers still use"} an older Credential version: ${skillBindingCount} Skill environment ${skillBindingCount === 1 ? "binding" : "bindings"}, ${mcpGrantCount} MCP ${mcpGrantCount === 1 ? "grant" : "grants"}, and ${systemModelCount} system ${systemModelCount === 1 ? "model" : "models"}.`,
      credentialRotationNote:
        "Replacement creates a new version only. Existing MCP Grants, Skill environment bindings, and system models stay pinned to the previous key until they are migrated explicitly.",
      historySchemaUnavailable:
        "The current Credential field schema could not be verified. Reload and try again.",
    },
    status: {
      active: "Active",
      archived: "Archived",
      suspended: "Suspended",
      draft: "Draft",
      pending_approval: "Pending approval",
      published: "Published",
      rejected: "Rejected",
      retired: "Replaced",
      revoked: "Revoked",
    },
    mcpToolInventory: {
      title: "Service tools",
      description:
        "Tools most recently discovered safely from this MCP service by a Worker.",
      toolCount: (count) => `${count} ${count === 1 ? "tool" : "tools"}`,
      loading: "Loading tool inventory",
      unpublished:
        "This configuration is not active yet. Edit it and bind a Project Credential; once active, a Worker will automatically test the service and read its tool inventory.",
      neverDiscovered:
        "No tool discovery has completed yet. You can test the service and read its tool inventory now.",
      testing: "Testing the service and reading tools…",
      catalogInvalid:
        "The latest discovery returned an unsafe tool inventory. Check the MCP service tool names, descriptions, and parameter schemas, then test again.",
      discoveryUnavailable:
        "The latest connection to the MCP service failed. Check service availability, outbound proxy, and network configuration, then test again.",
      stale:
        "The MCP configuration or Credential authorization changed, so the previous tool inventory is stale. Test again.",
      refreshFailed:
        "The tool inventory could not be refreshed. The last loaded result is still shown.",
      degradedSuffix: "The last successfully discovered tools are still shown.",
      empty:
        "The service returned no usable tools during its latest discovery.",
      lastSuccess: "Last successful discovery:",
      noDescription: "No description",
      testService: "Test service",
      retestService: "Test again",
      testingAction: "Testing…",
      testFailurePrefix:
        "The configuration was saved, but the service test failed.",
      loadErrors: {
        notFound:
          "This MCP configuration does not exist or is no longer visible. Close the details and refresh the MCP list.",
        forbidden:
          "You do not have permission to view this MCP tool inventory. Ask a project administrator to verify asset read access.",
        authRequired:
          "Your session expired. Sign in again to load the tool inventory.",
        responseInvalid:
          "The asset service returned an invalid tool inventory, so it was not displayed. Ask an administrator to check Gateway and frontend compatibility.",
        network:
          "The asset service is temporarily unavailable. Try again later or contact a platform administrator if the problem continues.",
        generic:
          "The tool inventory could not be loaded. Try again later or contact a project administrator if the problem continues.",
      },
      testErrors: {
        notFound:
          "This MCP configuration does not exist or is no longer visible.",
        forbidden: "You do not have permission to test this MCP service.",
        authRequired: "Your session expired. Sign in again before testing.",
        conflict:
          "The configuration or test state changed. Refresh and try again.",
        network: "The test request could not be submitted. Try again later.",
        generic: "The test request failed to submit. Try again later.",
      },
    },
    pages: {
      loading: "Loading assets",
      loadFailed: "Assets could not be loaded",
      systemCount: (count) => `${count} ${count === 1 ? "asset" : "assets"}`,
      emptySystem: (kind) =>
        `The packaged catalog contains no system ${kind} entries.`,
      emptyCreate: "Use the create action above to add the first entry.",
      system: {
        agentsTitle: "System Agents",
        skillsTitle: "System Skills",
        mcpTitle: "System MCP",
        credentialsTitle: "System Credentials",
      },
      projectLoadFailed: "Project assets could not be loaded",
      sourceCounts: (systemCount, projectCount) =>
        `System provided ${systemCount} · Project owned ${projectCount}`,
      project: {
        agentsTitle: "Project Agent governance",
        skillsTitle: "Project Skill governance",
        mcpTitle: "Project MCP governance",
        credentialsTitle: "Project Credential governance",
      },
    },
    catalog: {
      systemAssets: "System assets",
      systemAssetsDescription:
        "System assets are shared read-only. Project bindings pin an explicit version and never upgrade automatically.",
      systemCurrentAssetsDescription:
        "System Agents and Skills are shared read-only. Projects bind the asset and runtime resolves its Current Version.",
      systemMcpDescription:
        "System MCPs are shared read-only. Project bindings pin an explicit configuration and never switch automatically.",
      searchPlaceholder: "Search by name or identifier",
      filterAll: "All statuses",
      catalogReady: "Catalog loaded",
      totalAssets: "Total assets",
      activeAssets: "Active assets",
      unpublishedAssets: "Unpublished assets",
      assetsWithoutCurrentVersion: "Without Current Version",
      latestUpdate: "Latest update",
      noUpdate: "No update yet",
      publicationFilter: "Publication status",
      publicationAll: "All publication states",
      publishedOnly: "Published only",
      unpublishedOnly: "Unpublished only",
      updatedSort: "Updated-time sort",
      newestFirst: "Recently updated first",
      oldestFirst: "Oldest updated first",
      identifier: "Identifier",
      source: "Source",
      systemCatalogSource: "System catalog",
      lifecycleStatus: "Status",
      publicationStatus: "Publication",
      published: "Published",
      currentVersionStatus: "Version status",
      currentVersionAvailable: "Current Version available",
      currentVersionMissing: "No Current Version",
      assetRevision: "Asset revision",
      actions: "Actions",
      viewDetails: "View details",
      refresh: "Refresh catalog",
      refreshing: "Refreshing",
      resultRange: (from, to, total) =>
        `${from}–${to} of ${total} ${total === 1 ? "item" : "items"}`,
      page: (page, totalPages) => `Page ${page} of ${totalPages}`,
      previousPage: "Previous page",
      nextPage: "Next page",
      noResults: "No assets match the current filters.",
      noSystemAssets: "No visible system assets.",
      system: "System",
      systemPublishStatus: "System publication",
      pinnedVersion: "Pinned version",
      bindingStatus: "Binding status",
      bindingRevision: "Binding revision",
      publishedAvailable: "Published version available",
      unpublished: "Not published",
      enabledAndPinned: "Enabled and pinned",
      closed: "Disabled",
      notBound: "Not bound",
      enabled: "Enabled",
      none: "None",
      manageBinding: "Manage binding",
      projectAssets: "Project assets",
      projectAgentDescription:
        "Owned by this project. Saving Agent settings creates a Candidate Version; activating it makes it Current.",
      projectVersionedDescription:
        "Owned by this project. Content changes create immutable new versions.",
      noProjectAssets: "This project has no assets of this type.",
      project: "Project",
      publishStatus: "Publication",
      createNewVersion: "Create new version",
      credentialSource: "Credential source",
      systemCredentials: "System Credentials",
      projectCredentials: "Project Credentials",
      emptyCredentials: (title) => `No ${title}.`,
      waitingForAdmin: "Waiting for administrator approval",
      archive: "Archive",
      activate: "Enable",
      disable: "Disable",
      suspend: "Suspend",
    },
    version: {
      none: "No versions have been created.",
      mcpNone: "No configuration has been saved.",
      selectHint: "Select a version on the left to inspect its details.",
      number: (number) => `Version ${number}`,
      mcpNumber: (number) => `Configuration #${number}`,
      publish: "Publish version",
      publishMcp: "Publish configuration",
      submit: "Submit for approval",
      approve: "Approve and publish",
      approveMcp: "Approve and publish configuration",
      configureGrants: "Configure Credential grants",
    },
    diff: {
      payloadChecksum: "Payload checksum",
      description: "Description",
      model: "Model",
      toolGroups: "Tool groups",
      skillVersions: "Skill versions",
      mcpVersions: "MCP configurations",
      compatibility: "Compatibility",
      scanDecision: "Scan decision",
      scanAllow: "Allowed",
      scanWarn: "Warning",
      scanBlock: "Blocked",
      scanRules: "Scan rules",
      files: "Files",
      credentialRequirements: "Credential requirements",
      transport: "Transport",
      command: "Command",
      url: "URL",
      arguments: "Arguments",
      timeout: "Timeout",
      credentialSlots: "Credential slots",
      status: "Status",
      payloadSchemaVersion: "Payload schema version",
      payloadFields: "Payload fields",
      optional: "optional",
      required: "required",
      noDescription: "No description",
      seconds: (seconds) => `${seconds}s`,
      noChanges: "No structured changes.",
      field: "Field",
      previous: "Previous version",
      current: "Current version",
      previousMcpConfiguration: "Previous configuration",
      currentMcpConfiguration: "Current configuration",
    },
    runtime: {
      unsupportedProjectTransport:
        "Only SSE and HTTP are supported. This historical configuration can be viewed but cannot be published, bound, or used by an Agent.",
      unsupportedSystemTransport:
        "The Private runtime supports only stdio, SSE, and HTTP. This historical system configuration can be viewed but cannot be bound or used by an Agent.",
      missingProjectUrl:
        "This transport has no URL. The historical configuration can be viewed but cannot be published, bound, or used by an Agent.",
      invalidProjectUrl:
        "Project MCP requires an absolute HTTP or HTTPS URL without embedded credentials, query parameters, or fragments. The host must be exactly localhost or a canonical IPv4/IPv6 literal; ordinary DNS hostnames are not resolved. localhost is case-insensitive and is treated as 127.0.0.1; for IPv6 loopback, enter [::1] explicitly. The IP must belong to an administrator-configured allowed network range. The historical configuration can be viewed but cannot be published, bound, or used by an Agent.",
      projectOAuth:
        "Project MCP does not support configuration-level OAuth. The historical configuration can be viewed but cannot be published, bound, or used by an Agent.",
      projectHeadersOnly:
        "Project MCP Credential slots support headers or query parameters only. The historical configuration can be viewed but cannot be published, bound, or used by an Agent.",
      missingSystemCommand:
        "This stdio system MCP has no command and cannot be bound or used by an Agent.",
      missingSystemUrl:
        "This remote system MCP has no URL and cannot be bound or used by an Agent.",
      systemEnvOnly:
        "Stdio system MCP Credential slots support env only and cannot otherwise be bound or used by an Agent.",
      systemRemoteCredentialsOnly:
        "Remote system MCP Credential slots support headers, query parameters, or oauth only and cannot otherwise be bound or used by an Agent.",
    },
    dialogs: {
      addMcpTitle: "Add MCP",
      addMcpDescription:
        "Enter connection and authentication details. Secret values stay encrypted in Project Credentials.",
      addMcpSubmit: "Add MCP",
      addAndPublish: "Add and publish",
      addAndApprove: "Add, bind Credential, and publish",
      addAndSubmitApproval: "Add and save configuration",
      retryMcpApproval: "Retry binding and publication",
      mcpSavedApprovalFailed:
        "The MCP configuration was saved, but Credential binding and publication did not finish. Retry without creating another MCP.",
      mcpSavedRetryApprovalOnly:
        "The MCP is safely saved. Submitting again retries binding and publication only and will not create a duplicate.",
      addingMcp: "Adding…",
      editMcpConfigTitle: "Edit configuration",
      saveMcpConfig: "Save configuration",
      saveAndPublishMcpConfig: "Save and publish",
      saveAndApproveMcpConfig: "Save, bind Credential, and publish",
      saveAndSubmitMcpConfig: "Save configuration",
      savingMcpConfig: "Saving…",
      name: "Name",
      assetSlug: "Asset slug",
      slugTitle:
        "Use 3–63 lowercase letters, digits, and single hyphen separators",
      slugHelp: "3–63 lowercase letters, digits, or hyphens",
      filePath: "File path",
      mediaType: "Media type",
      fileContent: "File content",
      skillTemplateDescription: "Describe when and how to use this skill.",
      skillTemplateInstructions: "Add instructions for this skill here.",
      description: "Description",
      transport: "Transport",
      sseTransport: "Server-sent events (SSE)",
      httpTransport: "HTTP (Streamable HTTP)",
      mcpServiceUrl: "MCP service URL",
      urlQueryRemoved:
        "Query parameters were removed from the URL and their values will not be saved. Store them securely in a project Credential.",
      authentication: "Authentication",
      headerAuthentication: "Request header",
      queryAuthentication: "Query parameter",
      noAuthentication: "No authentication",
      noAuthenticationHelp:
        "This MCP does not read a Project Credential and can publish immediately.",
      connectionAndAuthentication: "Connection and authentication",
      needsProjectCredential: "Needs a project Credential",
      slotName: "Slot name",
      slotNameTitle:
        "Start with a lowercase letter and use only lowercase letters, digits, dots, underscores, or hyphens",
      slotNameHelp:
        "Start with a lowercase letter. Use at most 63 letters, digits, dots, underscores, or hyphens.",
      purpose: "Purpose",
      credentialFieldGroup: "Credential field group",
      requiredFields: "Required fields (comma or newline separated)",
      requiredFieldsHelp:
        "Enter the field names required in the selected Credential group. Separate multiple fields with commas or new lines.",
      requestHeaderName: "Request header name",
      queryParameterName: "Query parameter name",
      credentialFieldNameTitle:
        "Enter field names only. Separate multiple fields with commas; do not enter Basic, Bearer, or secret values.",
      credentialFieldNameHelp:
        "Enter field names only. Store secret values in a Project Credential; separate multiple fields with commas.",
      queryGroup: "Query parameters",
      unsupportedMcpTransport: "New MCP versions support SSE or HTTP only",
      missingMcpUrl: "SSE and HTTP transports require a URL",
      invalidMcpUrl:
        "Enter an HTTP or HTTPS endpoint reachable by the Worker without embedded credentials, query parameters, or fragments. The host must be exactly localhost or a canonical IPv4/IPv6 literal; ordinary DNS hostnames are not resolved. localhost is case-insensitive and is treated as 127.0.0.1; for IPv6 loopback, enter [::1] explicitly. The IP must belong to an administrator-configured allowed network range. Network ranges are configured at the platform level, not in this form.",
      mcpUrlQuery:
        "The URL cannot contain query parameters. Enter the base URL and store the secret through a query Credential slot.",
      unsupportedMcpCredentialGroup:
        "Project MCP Credential slots support request headers or query parameters only.",
      missingMcpCredentialSlotName:
        "Enter a slot name when Credential fields are provided.",
      missingMcpCredentialFields:
        "Enter at least one required field when a slot name is provided.",
      missingMcpHeaderName:
        "Enter a request header name, such as Authorization.",
      missingMcpQueryName: "Enter a query parameter name, such as key.",
      invalidMcpCredentialFieldName:
        "Enter only a request header or query parameter name. Do not paste Basic, Bearer, or secret values.",
      projectCredential: "Project Credential",
      createProjectCredential: "Create Project Credential",
      credentialSelectedByAdmin: "A project admin will select a Credential",
      noCompatibleCredential: "No matching Project Credential",
      compatibleCredentialsOnly:
        "Only enabled Credentials whose field structure exactly matches this authentication requirement are shown.",
      credentialFieldsMatch: "Fields match",
      adminCompletesApproval:
        "This account cannot bind Credentials. Save first, then let a project admin bind the Credential and activate the configuration.",
      safetyPreview: "Safety preview",
      configurationPreviewReadonly: "Configuration preview (read only)",
      serviceAddress: "Service address",
      waitingForServiceAddress: "Waiting for a service address",
      pendingCredentialSelection: "Waiting for a Project Credential",
      encryptedRead: "Encrypted read",
      secretNeverDisplayed:
        "Credential secrets remain encrypted and never appear in this form or preview.",
      credentialSource: "Credential source",
      encryptedProjectCredential: "Project Credential (encrypted)",
      publicationStatus: "Publication status",
      publishOnSave: "Publishes immediately after save",
      publishAfterApproval: "Binds and publishes automatically after save",
      publicationFlow: "Publication flow",
      saveMcpStep: "Save the MCP configuration",
      saveMcpStepDetail:
        "Connection details and authentication requirements are saved as an immutable configuration.",
      selectCredentialStep:
        "Select a Project Credential with an exact field match",
      selectCredentialStepDetail:
        "The group, field names, case, and order must match exactly.",
      approvePublishStep: "Bind the Credential and publish",
      approvePublishStepDetail:
        "The MCP configuration becomes active as soon as the Credential is bound.",
      approvalRunsAfterSave:
        "After a matching Credential is selected, saving automatically completes binding and publication.",
      createVersionTitle: (kind) => `Create ${kind} version`,
      secretCreateTitle: "Create Credential",
      secretReplaceTitle: "Replace Credential",
      secretDescription:
        "Secret values are used only for this encrypted write and are never returned after submission.",
      credentialSlug: "Credential slug",
      credentialSlugHelp:
        "Use 1–63 lowercase letters, numbers, dots, underscores, or hyphens; start and end with a letter or number.",
      credentialFields: "Credential fields",
      credentialFieldsHelp:
        "Add environment, header, query, or OAuth fields. Each secret value is written once.",
      fixedCredentialFieldsHelp:
        "The Credential type and field structure are fixed by the MCP authentication requirement. Enter only the name, slug, and secret value.",
      addField: "Add field",
      group: "Group",
      envGroup: "Environment (env)",
      headersGroup: "Headers",
      fieldName: "Field name",
      credentialValue: "Secret value",
      removeField: (index) => `Remove field ${index}`,
      remove: "Remove",
      writing: "Writing…",
      encryptWrite: "Encrypt and save",
      validation: {
        invalidCredentialName:
          "Use 1–63 lowercase letters, numbers, dots, underscores, or hyphens; start and end with a letter or number.",
        emptyFields: "Add at least one Credential field.",
        unsupportedGroup: "Select a supported Credential field group.",
        emptyField: "Enter a field name.",
        fieldTooLong: "Field names cannot exceed 255 characters.",
        duplicateField: "Fields cannot be duplicated within one group.",
        emptyValue: "Enter a secret value.",
      },
      revokeTitle: "Revoke Credential?",
      revokeDescription: (name) =>
        `This cannot be undone. Revoking “${name}” invalidates all Credential versions and related active Grants in one transaction. MCP details will no longer report them as authorized.`,
      cancel: "Cancel",
      revoking: "Revoking…",
      confirmRevoke: "Permanently revoke",
      migrateTitle: "Move existing consumers to the current Credential version",
      migrateDescription: (name) =>
        `These consumers still use an older version of “${name}”. Confirm to switch all of them to the current version. If any consumer is incompatible, the entire operation is cancelled.`,
      migrationDetailsTitle: "Consumers to update",
      migrationCurrentDetailsTitle: "Current-version consumers",
      migrationSkillBinding: "Skill environment variable",
      migrationMcpGrant: "MCP grant",
      migrationSystemModel: "System model",
      migrationVersion: (version) => `Version ${version}`,
      migrationTarget: "Usage",
      migrationSource: "Source field",
      migrating: "Switching…",
      confirmMigrate: "Confirm switch",
      deleteTitle: "Delete Credential?",
      deleteDescription: (name) =>
        `Deleting “${name}” removes all versions from ordinary lists and runtime queries. Related MCP Grants and Skill environment bindings become invalid. Only audit records remain. This cannot be undone.`,
      deleting: "Deleting…",
      confirmDeleteCountdown: (seconds) => `Confirm delete (${seconds}s)`,
      confirmDelete: "Confirm delete",
      binding: {
        switchTitle: "Switch project binding version",
        enableTitle: "Enable system asset",
        description: (name) =>
          `${name}. This changes only the current project binding. It never modifies the packaged system definition or version.`,
        selectPublished: "Select a published version",
        selectPublishedAria: "Select a published version",
        selectPlaceholder: "Select a version",
        unavailableSuffix: " (unavailable)",
        noBindableVersions: "No published versions can be bound.",
        currentProject: (version) => `Current project: ${version}`,
        notEnabled: "Not enabled",
        disable: "Disable for this project",
        enable: "Enable for this project",
        rollback: "Roll back to this version",
        switchVersion: "Switch to new version",
      },
      approval: {
        configureTitle: "Configure MCP Credential grants",
        configureDescription:
          "Select system Credentials for a published packaged system MCP. This configures slot grants only; it never modifies or republishes the MCP definition.",
        saveGrants: "Save grants",
        configureEmptyOptional:
          "No Credentials are eligible. Optional slots may remain empty to clear existing grants.",
        clearOptionalGrant: "No Credential",
        publishTitle: "Approve MCP configuration",
        publishDescription:
          "Select an enabled, currently visible Credential for each slot. The MCP configuration is published only after approval succeeds.",
        approve: "Approve and publish configuration",
        publishEmptyOptional:
          "No Credentials are eligible. Optional slots may remain empty for approval.",
        selectCredential: "Select a Credential",
        currentVersion: "Current Credential",
        loadingCredentials: "Loading Credentials…",
        credentialsFailed: "Credentials could not be loaded. Retry.",
        requiredUnavailable:
          "No eligible Credential is available for a required slot.",
      },
    },
    errors: {
      notFound: "The asset does not exist or is no longer visible.",
      forbidden: "This account cannot perform that action.",
      conflict: "The asset changed. Refresh and try again.",
      validationFailed:
        "The submitted content does not meet asset requirements.",
      mcpVersionValidation:
        "The MCP configuration failed validation. Confirm the transport is HTTP (Streamable HTTP) or SSE; the URL has no embedded credentials, query parameters, or fragments; the host is exactly localhost or a canonical IPv4/IPv6 literal rather than an ordinary DNS hostname; localhost is case-insensitive and is treated as 127.0.0.1, while IPv6 loopback is entered explicitly as [::1]; the IP belongs to an administrator-configured allowed network range; and every Credential slot uses exactly one headers or query group with fields. Network ranges are configured by platform administrators, not in this form. If an administrator just changed the allowed ranges, restart Gateway, Scheduler, and Worker.",
      mcpCredentialMismatch:
        "The selected Credential does not satisfy the MCP slot or is no longer active. Its group and field names must exactly match the selected slot schema, including case.",
      storageQuota:
        "The project Skill storage quota is exhausted. Remove unused Skills and try again.",
      storageUnavailable:
        "Asset storage is temporarily unavailable. Try again.",
      authRequired: "The session has expired. Sign in again.",
      network: "The asset service is temporarily unreachable. Try again.",
      invalidResponse: "The asset service returned invalid data.",
      invalidErrorResponse: "The operation failed. Try again.",
      fallback: "The operation failed. Try again.",
    },
  },

  automation: {
    title: "Automations",
    create: "Create automation",
    editTitle: "Edit automation",
    deleteTitle: "Delete automation",
    emptyTitle: "No automations yet",
    emptyDescription:
      "After creating one, project Agents can run on a cron or one-time schedule.",
    filterEmptyTitle: "No automations match these filters",
    selectPrompt: "Select an automation.",
    runNow: "Run now",
    schedulerDisabled: "Scheduling is disabled",
    migrationRequired: "Automation migration is required",
    retry: "Retry",
    history: "Run history",
    fields: {
      title: "Title",
      prompt: "Prompt",
      schedule: "Schedule",
    },
  },

  // Scheduled tasks
  scheduledTasks: {
    description: "Run tasks on a schedule and review every result.",
    migrationComplete: {
      title: "Automation migration is complete",
      description:
        "This legacy scheduled-task page is closed. Open a project to manage Automations.",
    },
    scheduleType: {
      cron: "Recurring",
      once: "One-time",
    },
    preset: {
      label: "Repeat",
      hourly: "Hourly",
      daily: "Daily",
      weekly: "Weekly",
      monthly: "Monthly",
      custom: "Custom cron",
    },
    fields: {
      minute: "Minute",
      time: "Time",
      weekday: "On",
      dayOfMonth: "Day of month",
      cron: "Cron expression",
      cronPlaceholder: "0 9 * * *",
      runAt: "Run at",
      timezone: "Timezone",
    },
    weekdays: {
      mon: "Mon",
      tue: "Tue",
      wed: "Wed",
      thu: "Thu",
      fri: "Fri",
      sat: "Sat",
      sun: "Sun",
    },
    preview: "Preview",
    cronHelp: "Open crontab.guru",
    create: {
      title: "Create scheduled task",
      taskTitle: "Task title",
      prompt: "Prompt",
      submit: "Create",
      fillRequired: "Fill all required fields",
    },
    context: {
      fresh: "Fresh thread",
      reuse: "Reuse thread",
      threadIdPlaceholder: "Thread ID",
    },
    filters: {
      status: "Status",
      type: "Type",
      all: "All",
      allStatuses: "All statuses",
      enabled: "Enabled",
      paused: "Paused",
      completed: "Completed",
      failed: "Failed",
      allTypes: "All types",
      cron: "Cron",
      once: "Once",
    },
    empty: {
      title: "No scheduled tasks yet",
      description:
        "Create a task and let ActWeave automatically complete work on schedule.",
      action: "Create your first task",
      filteredTitle: "No tasks match your filters",
      filteredDescription:
        "Adjust your filters or clear them to see all tasks.",
      clearFilters: "Clear filters",
    },
    detail: {
      contextMode: "Context mode",
      thread: "Thread",
      lastThread: "Last thread",
      schedule: "Schedule",
      nextRun: "Next run",
      lastRun: "Last run",
      lastRunId: "Last run id",
      lastError: "Last error",
      runCount: "Total runs",
      runsCount: "{count} runs",
      runsCountOne: "{count} run",
      noRuns: "No runs yet",
      noSelection: "No scheduled task selected",
      filteredByThread: "Filtered by thread: {id}",
      loadFailed: "Failed to load scheduled tasks",
    },
    actions: {
      edit: "Edit",
      cancelEdit: "Cancel edit",
      pause: "Pause",
      resume: "Resume",
      trigger: "Trigger now",
      delete: "Delete",
    },
    deleteConfirm:
      "Are you sure you want to delete this scheduled task? This action cannot be undone.",
    errors: {
      create: "Failed to create scheduled task",
      update: "Failed to update scheduled task",
      pause: "Failed to pause scheduled task",
      resume: "Failed to resume scheduled task",
      trigger: "Failed to trigger scheduled task",
      delete: "Failed to delete scheduled task",
    },
    edit: {
      titlePlaceholder: "Edit title",
      promptPlaceholder: "Edit prompt",
      submit: "Save edit",
    },
    status: {
      enabled: "Enabled",
      paused: "Paused",
      running: "Running",
      completed: "Completed",
      failed: "Failed",
      cancelled: "Cancelled",
    },
    runTrigger: { scheduled: "scheduled", manual: "manual" },
    runStatus: {
      queued: "Queued",
      running: "Running",
      success: "Success",
      failed: "Failed",
      skipped: "Skipped",
      interrupted: "Interrupted",
    },
    recipes: {
      label: "Quick create",
      trending: {
        title: "GitHub Trending daily",
        desc: "Summarize today's top 10 trending repos",
      },
      news: {
        title: "Daily tech news digest",
        desc: "Collect and summarize the day's top tech news",
      },
      issues: {
        title: "GitHub Issue triage",
        desc: "Triage a repo's open issues (fill in {{repo}})",
      },
      weekly: {
        title: "Weekly report",
        desc: "Compile a weekly summary, every Monday",
      },
    },
  },

  // Agents
  agents: {
    common: {
      cancel: "Cancel",
      close: "Close",
      retry: "Retry",
      retrying: "Retrying…",
      send: "Send",
      system: "System",
      project: "Project",
      defaultSuffix: "Default",
      count: (value) => `${value}`,
    },
    builder: {
      errors: {
        unavailable:
          "The Agent design service is unavailable. Try again later.",
        conflict: "An Agent with that name already exists in this project.",
        forbidden: "Your account cannot create Agents.",
        notFound: "This Agent design session no longer exists.",
        validationFailed:
          "The submitted content is invalid. Review it and try again.",
        invalidResponse:
          "The model returned an invalid result. Retry this operation.",
        network:
          "Could not reach the Agent design service. Check your connection and retry.",
        slugConflict:
          "An Agent in this project already uses this name. Change the Agent name in the blueprint and retry.",
        unresolvedConflict:
          "The blueprint still has unresolved conflicts. Continue the conversation and have the Agent regenerate it before creating the Agent.",
        sessionLimitExceeded:
          "You have reached the unfinished Agent design limit. Resume or cancel an existing design before starting another.",
        secretDetected:
          "The input appears to contain a secret or other sensitive value. Remove it and retry.",
        commitUncertain:
          "The creation result could not be confirmed. Do not create a duplicate. Check the Agent list first, then retry only if it is absent.",
        stale:
          "The design changed on the server. Reload the latest state before continuing.",
      },
      start: {
        title: "Name your new Agent",
        hint: "Use letters, numbers, and hyphens. Input is stored in lowercase (for example, code-reviewer).",
        nameLabel: "Agent name",
        placeholder: "For example, code-reviewer",
        savedAs: (value) => `Will be saved as ${value}`,
        creating: "Creating…",
        continue: "Continue",
        forbidden: "Your account cannot create Agents.",
        nameTooShort: "The name must contain at least 3 characters",
        nameTooLong: "The name cannot exceed 63 characters",
        nameInvalid: "Use lowercase letters, numbers, and single hyphens only",
      },
      progress: {
        stepsAria: "Design steps",
        designing: "Designing Agent…",
        steps: "Design steps",
        settingsProgressAria: "Progress for the four Agent settings",
      },
      resume: {
        title: "Continue unfinished Agent designs",
        lastUpdated: (value) => `Last updated ${value}`,
        deleteAria: (name) => `Delete unfinished Agent: ${name}`,
        deleteTitle: "Delete unfinished Agent?",
        deleteDescription: (name) =>
          `This deletes the design session for “${name}”, so it cannot be continued. Existing Agents are not affected.`,
        deleting: "Deleting…",
        confirmDelete: "Delete session",
      },
      blueprint: {
        result: "Generated result",
        title: "Agent blueprint",
        description:
          "Review the generated settings and Agent name before creating the Agent.",
        runtime: "Runtime configuration",
        noDescription: "No Agent description has been generated.",
        nameLabel: "Agent name",
        nameHint:
          "The created Agent uses this value for both its name and slug.",
        savedAs: (value) => `Normalized as: ${value}`,
        model: "Model",
        capabilities: "Capabilities and dependencies",
        dependencySummary: (toolGroups, skills, mcps) =>
          `${toolGroups} tool groups · ${skills} Skills · ${mcps} MCPs`,
        checkingMcp: "Checking MCP dependencies…",
        modelUnavailable: "Agent model unavailable",
        modelRecovery:
          "Continue the conversation and ask the Agent to use a currently available model before creating it.",
        assumptionsTitle: "Design assumptions",
        conflictsTitle: "Conflicts to resolve",
        conflictDocuments: "Related documents",
        blockingConflictHint:
          "Continue the conversation so the Agent regenerates the blueprint. The Agent cannot be created until all red conflicts disappear.",
        createHint:
          "Saves immutable candidate version v1. Activate it before it can run.",
        creating: "Creating…",
        createDraft: "Create Agent",
        validation: {
          descriptionRequired: "Agent description is required",
          modelRequired: "Agent model is required",
          toolGroupRequired: "Agent requires at least one valid tool group",
          documentRequired: (name) => `${name} is required before saving`,
        },
      },
      conversation: {
        permissionReadOnly:
          "Your account cannot continue designing this Agent. Saved session content remains available to view.",
        newAgentIntro: (name) =>
          `The new Agent is named ${name}. Use the conversation below to describe its purpose, behavior, and collaboration boundaries.`,
        creatingAgent: "Creating Agent…",
        designingAgent: "Designing Agent…",
        composerAria: "Describe the Agent you want",
        saveLocalChangesFirst: "Save or discard the changes above first",
        answerQuestionFirst: "Answer the question above first",
        generatingBlueprint: "Generating Agent blueprint…",
        composerPlaceholder:
          "Describe the Agent you want and I will help design it through conversation.",
        loadingModels: "Loading conversation models…",
        modelLoadFailed: "Conversation models could not be loaded.",
        noModels: "No conversation models are available.",
        selectModelAria: "Select the model used to design the Agent",
        selectModel: "Select conversation model",
        modelLabel: "Conversation model used to design the Agent",
        backToAgents: "Continue later and return to Agents",
        designAgent: "Design Agent",
        autosave: "Automatically saved; continue later",
        more: "More actions",
        abandon: "Abandon this design",
        conversationAria: "Agent design conversation",
        sessionUnavailable: "The Agent design session is unavailable.",
        abandonTitle: "Abandon this Agent design?",
        abandonDescription:
          "This design session will end and disappear from the unfinished list. Existing Agents are not affected.",
        continueDesign: "Continue designing",
        abandoning: "Abandoning…",
        confirmAbandon: "Abandon design",
        discardTitle: "Discard unsaved changes?",
        discardDescription:
          "The Agent design session remains available, but local changes to the four settings will be discarded.",
        continueEditing: "Continue editing",
        discardAndLeave: "Discard changes and leave",
      },
    },
    instructions: {
      files: {
        agents: "Collaboration rules and task boundaries",
        soul: "Personality, voice, and values",
        identity: "Name, role, and identity",
        user: "Long-term user context",
      },
      sectionAria: "Agent instruction documents",
      title: "Instruction documents",
      draftDescription:
        "Edit the four fixed Markdown documents and save an immutable Agent Candidate Version.",
      blueprintDescription:
        "Review and edit the four fixed Markdown documents.",
      readOnlyDescription:
        "Read-only instruction documents for the System Agent's sole v1.",
      edit: "Edit",
      fixedFiles: "Fixed files",
      displayMode: "Display mode",
      source: "Source",
      preview: "Preview",
      empty: "No content",
      editFile: (name) => `Edit ${name}`,
      draftSaveHint:
        "Saving creates a new Agent Candidate Version without activating it.",
      blueprintSaveHint:
        "Saving updates the current blueprint before the Agent can be created.",
      discard: "Discard changes",
      saving: "Saving…",
      save: "Save Candidate Version",
      permissionLost:
        "Editing permission was revoked. Local changes are preserved but cannot be saved yet.",
      recoveryPreserved:
        "The server version changed. Local changes were preserved; review them before saving again.",
      recoverySynced:
        "The server version changed and the latest version has been loaded.",
      recoveryFailed:
        "The latest version could not be loaded. Local changes were preserved.",
      recoveryReloading: "Loading the latest version…",
      invalidResponse: "The server returned an invalid Agent version.",
      conflictDetected: "A version conflict was detected.",
      reloadRequired: "Reload the latest version before editing.",
      reloading: "Reloading…",
      reload: "Reload",
      discardTitle: "Discard instruction changes?",
      discardDescription:
        "Changes to the four instruction documents will not be saved.",
      continueEditing: "Continue editing",
    },
    capabilities: {
      reasons: {
        archived: "Asset is archived",
        inactive: "Asset is inactive",
        bindingDisabled: "System binding is disabled",
        bindingMissing: "System binding is missing",
        noPublishedVersion: "No Current Version",
      },
      remediation: {
        restoreSystemAsset: "Ask an administrator to restore the system asset",
        enableSystemBinding:
          "Ask an administrator to enable the system binding",
        publishVersion:
          "Activate a Candidate Version of this project asset first",
        activateProjectAsset: "Activate this project asset first",
      },
      explanationSeparator: "; ",
      boundCount: (value) => `${value} bound`,
      unavailablePrefix: (reason) => `Unavailable: ${reason}`,
      remediationPrefix: (reason) => `Next step: ${reason}`,
      historicalDisabled:
        "Capability bindings cannot be edited on historical versions.",
      historicalVersion: "Historical version",
      historicalVersionDescription:
        "Capability bindings are read-only for this historical version.",
      permissionLost:
        "Editing permission was revoked. Local changes are preserved but cannot be saved yet.",
      recoverySynced:
        "The server version changed and the latest version has been loaded.",
      recoveryPreserved:
        "The server version changed. Local changes were preserved; review them before saving again.",
      recoveryFailed:
        "The latest version could not be loaded. Local changes were preserved.",
      recoveryReloading: "Loading the latest version…",
      conflictDetected: "A version conflict was detected.",
      reloadRequired: "Reload the latest version before editing.",
      reloading: "Reloading…",
      permissionBlocked: "Your account cannot edit Agent capabilities.",
      preparingDraft: "Preparing the Candidate Version…",
      catalogLoading: "Loading capability catalog…",
      catalogLoadFailed: "Capability catalog could not be loaded.",
      validatingMcp: "Validating MCP dependencies…",
      mcpValidationFailed: "MCP dependency validation failed.",
      title: "Capability bindings",
      description:
        "Choose the tool groups, Skills, and MCPs this Agent can use.",
      saving: "Saving…",
      saveDraft: "Save Candidate Version",
      edit: "Edit",
      builtinGroups: "Built-in tool groups",
      unchanged: "Unchanged",
      searchPlaceholder: "Search Skills or MCPs",
      searchAria: "Search capability catalog",
      catalogLoadingStatus: "Loading capability catalog…",
      catalogLoadFailedStatus: "Capability catalog could not be loaded.",
      emptySkills: "No Skills are available.",
      emptyMcps: "No MCPs are available.",
      reload: "Reload",
    },
    catalog: {
      title: "Agents",
      authoringLoadFailed: "The Agent authoring base could not be loaded.",
      authoringLoading: "Loading the Agent authoring base…",
      detailTabsAria: "Agent detail tabs",
      instructionsTab: "Instructions",
      capabilitiesTab: "Capabilities",
      viewModeAria: "Agent catalog view",
      cards: "Cards",
      list: "List",
      chatForbidden: "Your account cannot create chats in this project.",
      unavailable: "This Agent is unavailable.",
      executeForbidden: "Your account cannot run Agents.",
      publishRequired:
        "The Agent needs a Current Version and the asset must be active.",
      defaultAdminOnly: "Only administrators can set the default Agent.",
      defaultUnavailable: "This Agent cannot be set as default.",
      systemDefaultUnavailable:
        "Enable this system Agent in the project first.",
      mainUnavailable: "The main Agent is unavailable.",
      mainExecuteForbidden: "Your account cannot run the main Agent.",
      mainVersionUnavailable: "The main Agent has no available version.",
      emptySystem: "No system Agents are available.",
      emptyProject: "No project Agents yet.",
      defaultLoadFailed: "The default Agent could not be loaded.",
      defaultLoading: "Loading default Agent…",
      defaultUnknown: "Default Agent unavailable",
      setDefaultBlockedAria: (name, reason) =>
        `Cannot set ${name} as default: ${reason}`,
      setDefaultAria: (name) => `Set ${name} as default Agent`,
      settingDefault: "Setting…",
      setDefault: "Set as default",
      activateAria: (name) => `Activate ${name}`,
      activating: "Activating…",
      activate: "Activate",
      chatBlockedAria: (name, reason) => `Cannot chat with ${name}: ${reason}`,
      chatAria: (name) => `Start a new chat with ${name}`,
      creatingChat: "Creating…",
      chat: "Chat",
      builtIn: "Built in",
      currentDefault: "Current default",
      suspended: "Inactive",
      viewDetails: (name) => `View ${name} details`,
      mainDescription: "The project's built-in main Agent.",
      noDescription: "No description.",
      mcpValidationFailed: "MCP dependency validation failed.",
      systemSection: "System Agents",
      systemDescription:
        "Agents provided by the platform and enabled for this project.",
      projectSection: "Project Agents",
      projectDescription: "Agents designed and managed in this project.",
      createChatFailed: "Could not create the Agent chat.",
      activated: (name) => `${name} activated`,
      defaultSet: (name) => `${name} is now the default Agent`,
      mainDefaultSet: "The main Agent is now the default",
    },
    selector: {
      title: "Choose an Agent",
      description: "Choose an available Agent for the new chat.",
      emptyTitle: "No Agents available",
      emptyDescription: "This project has no Agent that can start a chat.",
      loading: "Loading Agents…",
      loadFailed: "Agents could not be loaded.",
      projectAgent: "Project Agent",
      systemAgent: "System Agent",
      enableNow: "Enable now",
      enableAndChat: (name) => `Enable ${name} and start a chat`,
      dependencyTitle: "Dependencies not ready",
      dependencyDescription: "This Agent's dependencies are unavailable.",
      mcpBlockedTitle: "MCP dependencies not ready",
      mcpBlockedDescription:
        "Fix the MCP configuration before starting a chat.",
      configure: "Open configuration",
      createProjectAgent: "Create project Agent",
      contactEditor: "Contact project editor",
      alternateTitle: "Choose another Agent",
      alternateDescription: "Choose another available Agent for a new chat.",
      alternateEmptyTitle: "No other Agent available",
      alternateEmptyDescription:
        "No other Agent is currently available for a new chat.",
      dependencyLoadFailed: "Agent dependency status could not be loaded.",
      createChatFailed: "Could not create the Agent chat.",
      enableFailed: "Could not enable the system Agent.",
      systemUnavailable: "This system Agent is unavailable.",
    },
    startContinuation: {
      waitingForService: {
        title: "Waiting for the service",
        detail: "The system will keep trying to create the chat.",
      },
      waitingForAgent: {
        title: "Waiting for the Agent",
        detail: "The chat will be created after the Agent is ready.",
      },
      creatingChat: {
        title: "Creating chat",
        detail: "Please wait and do not submit again.",
      },
      readOnly: {
        title: "Action unavailable",
        detail: "Your account lacks the permissions required to create a chat.",
      },
      error: {
        title: "Could not create chat",
        detail: "Review the Agent status and try again.",
      },
      retryChat: "Retry chat creation",
      configuredRetry: "Retry after configuration",
      defaultLoadFailed: "The default Agent could not be loaded.",
      dependencyFailed: "Agent dependencies could not be checked.",
    },
    newChat: {
      threadName: "New chat",
      defaultUnknown: "Default Agent unavailable",
      mainUnavailable: "Main Agent unavailable",
      projectUnavailable: "Project default Agent unavailable",
      loadDefaultFailed: "The default Agent could not be loaded.",
      dependencyFailed: "Agent dependencies could not be checked.",
      createFailed: "Could not create a new chat.",
      defaultAdmissionUnavailable:
        "The default Agent cannot start a chat right now.",
    },
    indicator: {
      unavailable: "Current Agent unavailable",
      label: "Current Agent",
      startWithOther: (current) =>
        `Currently using ${current}. Choose another Agent to start a new chat`,
      current: (current) => `Current Agent: ${current}`,
    },
  },

  skills: {
    export: {
      label: "Export ZIP",
      preparing: "Preparing…",
      started: "Download started",
      tooltip: (version) => `Export the selected v${version}`,
      unsaved: "Save or discard the current changes first",
      loading: "The selected version is still loading",
      revoked: "Revoked System Skill versions cannot be exported",
      unpublished: "Only the System Skill Current Version can be exported",
      noVersion: "Select a persisted version first",
    },
    secrets: {
      workbenchAria: "Skill editing area",
      filesTab: "Files",
      secretsTab: "Runtime credentials",
      aria: "Environment variable declarations",
      title: "1. Environment variable declarations",
      viewSource: "View SKILL.md",
      checking: "Checking SKILL.md…",
      syncing: "Writing unsaved SKILL.md changes…",
      recognized: (count) =>
        `${count} environment variable${count === 1 ? "" : "s"} recognized from SKILL.md`,
      draftUpdated: "Written to SKILL.md; changes are not saved yet",
      retry: "Check again",
      sourceStale:
        "SKILL.md changed while it was being processed. Your current edits were preserved; try again.",
      invalidDeclaration:
        "The environment variable declaration is invalid. Open the source and follow the validation details.",
      forbidden: "You cannot edit this Skill declaration.",
      notFound: "This project no longer exists.",
      invalidResponse:
        "The Skill declaration service returned an invalid response, so no change was applied.",
      unavailable:
        "The Skill environment variable declaration cannot be processed right now. Try again later.",
      invalidSource:
        "The SKILL.md frontmatter is invalid. The source was preserved; fix it in source view and try again.",
      openSource: "Open source to fix",
      managedComments:
        "The managed fields contain comments that cannot be preserved safely. The form is read-only; edit the source instead.",
      shorthand: (count) =>
        `${count} legacy shorthand declaration${count === 1 ? " was" : "s were"} found. They are normalized only after an actual edit.`,
      empty:
        "No environment variables are declared yet. Add one to update the unsaved SKILL.md changes.",
      beginEdit: "Create a new version and add a variable",
      optional: "Optional",
      required: "Required",
      setOptional: (name) => `Make ${name} optional`,
      remove: (name) => `Remove the ${name} declaration`,
      addTitle: "Add an environment variable declaration",
      nameLabel: "Variable name",
      namePlaceholder: "API_KEY",
      newOptional: "Make the new variable optional",
      add: "Add",
      invalidName:
        "The name must start with a letter or underscore and contain only letters, numbers, and underscores.",
      duplicateName:
        "That environment variable is already declared. Names are case-sensitive.",
      autonomousTitle: "Credential injection mode",
      autonomousDescription:
        "Choose which Skill activation modes may inject already bound and authorized Credentials.",
      autonomousAria: "Choose the Credential injection mode",
      advancedSettings: "Advanced settings",
      injectionAutomatic: "Inject after the Skill is read",
      injectionAutomaticDescription:
        "The Agent may inject bound Credentials after reading this Skill. Explicit /Skill activation also injects them.",
      injectionExplicit: "Inject only after explicit /Skill activation",
      injectionExplicitDescription:
        "Natural-language automatic loading does not inject Credentials. Injection requires explicit /Skill activation.",
      location: (line, column) =>
        `Line ${line}${column === null ? "" : `, column ${column}`}: `,
      loadSourceFailed:
        "The root SKILL.md could not be loaded, so environment variable editing is unavailable.",
      loadSource: "Loading SKILL.md…",
      saveBlocked:
        "Wait for the SKILL.md check to finish or fix the environment variable declaration",
      publishBlocked:
        "Wait for the SKILL.md check to finish and fix the environment variable declaration before activation.",
      credentialLabel: "Project Credential",
      noCompatibleCredential: "No compatible Credential",
      optionalUnbound: "Leave unbound (optional)",
      selectCredential: "Select a Credential",
      credentialVersion: (name, version) => `${name} · version ${version}`,
      credentialUnavailable: "Unavailable Credential · select a replacement",
      versionLabel: (version) => `version ${version}`,
      sourceFieldLabel: "Source environment variable",
      selectCredentialFirst: "Select a project Credential first",
      selectSourceField: "Select an env field from the Credential",
      sourceFieldUnavailable: "field no longer available",
      sourceFieldRequired: "Select a specific source env field.",
      requiredMissing: "Select a Credential for this required variable.",
      invalidMapping:
        "This mapping is no longer compatible. Select an available Credential and source env field.",
      createCredential: "Create Credential",
      manageCredential: "Manage project Credentials",
      mappingTitle: "2. Project Credential mappings",
      mappingDescription:
        "Map each Skill environment variable to a specific env field in a project Credential. Mappings are stored in the project database, never in SKILL.md, and secret values are never displayed.",
      mappingEmpty:
        "This version declares no environment variables, so no project Credential mapping is needed.",
      mappingStatusConfigured: "Configured",
      mappingStatusMissing: "Not configured",
      mappingStatusInvalid: "Needs repair",
      mappingReadOnly:
        "You can edit the declarations in SKILL.md, but a member with Credential approval permission must configure their project Credential sources.",
      mappingHistoricalReadOnly:
        "Historical Versions are read-only. Candidate Versions and the Current Version support project Credential mappings.",
      mappingRefreshPreserved:
        "Server mappings changed. Your unsaved choices were preserved and merged with unchanged rows. Review them before saving or discarding.",
      mappingReload: "Reload",
      mappingDiscard: "Discard changes",
      mappingSave: "Save mappings",
      mappingSaving: "Saving…",
      mappingCompleteRequired:
        "Configure every required variable before saving mappings for an active Skill.",
      mappingRepairInvalid:
        "Repair invalid mappings or remove optional mappings before saving.",
      mappingLoadingAria: "Loading this version's project Credential mappings",
      mappingVersionMismatch:
        "The returned Credential mappings do not belong to this version, so they were not displayed. Reload and try again.",
      mappingRetry: "Try again",
      mappingConflict:
        "Credential mappings were changed by someone else, or this version is no longer editable. Your local choices were preserved; reload and review them.",
      mappingForbidden: "You cannot modify these Credential mappings.",
      mappingNotFound: "This Skill or version no longer exists.",
      mappingInvalidResponse:
        "The Credential mapping response was invalid, so it was not displayed to protect sensitive information.",
      mappingLoadFailed: "Credential mappings could not be loaded.",
      mappingSaveFailed:
        "Credential mappings could not be saved. Try again later.",
      mappingVersionChanged:
        "The current version changed. Your unsaved choices are still preserved, but saving is paused; reload and confirm them again.",
    },
    activationDialog: {
      title: "Activate Skill Candidate Version",
      description: (version) =>
        `Activate version ${version}. This dialog checks the exact version's environment variables and project Credential mappings.`,
      loading: "Checking runtime requirements…",
      targetVersion: "Target version: ",
      bindingRevision: "Binding revision: ",
      noRequirements:
        "The target version declares no environment variables and can be activated directly.",
      bindingsTitle: "Version Credential bindings",
      required: "Required",
      optional: "Optional",
      statusConfigured: "Configured",
      statusMissing: "Not configured",
      statusInvalid: "Needs repair",
      preflightReady: "Runtime requirement checks passed",
      preflightBlocked: "Runtime requirement checks did not pass",
      preflightSummary: (configuredRequired, required, invalid) =>
        `${configuredRequired}/${required} required mappings configured; ${invalid} invalid mapping${invalid === 1 ? "" : "s"}.`,
      configureBeforeActivation:
        "Return to Runtime credentials, complete required mappings, and repair invalid mappings before activation.",
      configureCredentials: "Go to Runtime credentials",
      noApprove:
        "You cannot choose Credential sources. Ask a member with Credential approval permission to complete the mappings in Runtime credentials first.",
      approvalRequiredForActive:
        "Every Candidate Version must have all required mappings configured and invalid mappings repaired before activation. Ask an Admin to configure them first.",
      optionalUnbound: (count) =>
        `${count} optional variable${count === 1 ? " is" : "s are"} unbound and will not be injected at runtime.`,
      staleBase:
        "This version is not a forward descendant of the Current Version and cannot be activated.",
      credentialChanged:
        "Credentials changed. Compatible selections were preserved; review them and activate again.",
      assetChanged: "The Skill changed. Check its runtime requirements again.",
      cancel: "Cancel",
      retry: "Check again",
      activating: "Activating…",
      activate: "Activate version",
      confirmOverwrite: "Activate version",
      discardTitle: "Discard unsaved Credential selections?",
      discardDescription:
        "Closing clears selections made in this dialog. The Skill version and existing bindings are unchanged.",
      continue: "Continue configuring",
      discard: "Discard and close",
      createdInvalid:
        "The Credential was created, but its returned version metadata is invalid.",
      createdIneligible:
        "The Credential was created, but the latest runtime check does not consider its version compatible. Check its fields and try again.",
      incomplete:
        "Required environment variables for the active Skill are not fully bound. Complete the selections and try again.",
      invalidBinding:
        "The selected Credential is incompatible with the Skill declaration. Refresh the runtime check and try again.",
      staleSelection:
        "A Credential was rotated, suspended, or revoked. Available options were reloaded.",
      staleActivationBase:
        "This version is not a forward descendant of the Current Version and cannot be activated.",
      invalidDeclaration:
        "The target version has an invalid environment variable declaration. Create a new version and fix SKILL.md before activation.",
      forbidden: "You cannot activate this Skill.",
      credentialRequestOmitted:
        "You cannot select Credentials, so this activation request will not submit bindings.",
    },
    builder: {
      errors: {
        unavailable:
          "The Skill design service is unavailable. Try again later.",
        modelUnavailable:
          "The selected model is unavailable. Choose another model.",
        effortUnsupported:
          "The selected model does not support extended thinking. Adjust thinking intensity.",
        conflict: "A Skill with that name already exists in this project.",
        forbidden: "Your account cannot create Skills.",
        notFound: "This Skill design session no longer exists.",
        limitExceeded:
          "You have reached the unfinished Skill design session limit. Continue or abandon an existing session first.",
        validationFailed:
          "The candidate files did not pass checks. Fix them and try again.",
        invalidResponse:
          "The Skill design service returned an unexpected result. Try again.",
        network:
          "Could not reach the Skill design service. Check your connection and retry.",
        commitUncertain:
          "The creation result could not be confirmed. Do not create a duplicate. Check the Skill list first, then retry only if it is absent.",
        stale:
          "The candidate files changed on the server. Reload the latest state and try again.",
        targetDeleted:
          "The target Skill was deleted, so this revision session cannot continue. Return to the Skill list to create a new one.",
        targetDeletedBanner:
          "The target Skill was deleted, so this revision session cannot continue. Return to the Skill list to create a new one, or abandon this session.",
        targetDeletedStatus:
          "The target Skill was deleted; this revision cannot continue",
        noChanges:
          "The candidate files match the baseline exactly, so a new version is not needed. Edit a file before submitting.",
        baseStale:
          "The Current Version changed. This revision cannot be saved; start a new revision session from the Current Version.",
        targetSessionExists:
          "This Skill already has an unfinished revision session. Continue or abandon it from the unfinished list above the Skill list.",
        targetUnsupported:
          "The Current Version cannot be opened for conversational revision (size or content is outside Builder support). Create a new version by editing files instead.",
        targetConflict:
          "The Skill changed (it may have been deleted, archived, or have no Current Version). Refresh and try again.",
        attachmentTooLarge: "Each attachment must be 256 KB or smaller.",
        attachmentNotUtf8: (name) => `“${name}” is not a UTF-8 text file.`,
        attachmentInvalidName:
          "The attachment name contains unsupported characters. Rename it and try again.",
        attachmentTooMany: (max) =>
          `You can attach at most ${max} reference files at a time.`,
        attachmentTotalTooLarge:
          "Attachments together must be 512 KB or smaller.",
        packageTooLarge: "The candidate file package must be 2 MiB or smaller.",
        fileTooLarge: "Each candidate file must be 512 KiB or smaller.",
      },
      start: {
        title: "Name your new Skill",
        hint: "The name becomes the immutable SKILL.md frontmatter name and is stored as lowercase hyphenated text.",
        nameLabel: "Skill name",
        placeholder: "For example, paper-review",
        savedAs: (value) => `Will be saved as ${value}`,
        creating: "Creating…",
        continue: "Continue",
        forbidden: "Your account cannot create Skills.",
        nameTooShort: "The name must contain at least 3 characters",
        nameTooLong: "The name cannot exceed 63 characters",
        nameInvalid: "Use lowercase letters, numbers, and single hyphens only",
      },
      resume: {
        titleCreate: "Continue unfinished Skill creation",
        titleRevise: "Continue unfinished revisions",
        titleMixed: "Continue unfinished Skill designs",
        kindCreate: "Create",
        kindRevise: "Revise",
        lastUpdated: (value) => `Last updated ${value}`,
        deleteAriaCreate: (name) => `Delete unfinished Skill: ${name}`,
        deleteAriaRevise: (name) => `Delete unfinished revision: ${name}`,
        deleteTitleCreate: "Delete unfinished Skill?",
        deleteTitleRevise: "Delete unfinished revision?",
        deleteDescriptionCreate: (name) =>
          `This deletes the design session for “${name}”, so it cannot be continued. Existing Skills are not affected.`,
        deleteDescriptionRevise: (name) =>
          `This deletes the revision session for “${name}”, so it cannot be continued. Saved Skill versions are not affected.`,
        deleting: "Deleting…",
        confirmDelete: "Delete session",
      },
      revision: {
        button: "AI revise",
        opening: "Opening revision session…",
        saveLocalChangesFirst: "Save or discard unsaved changes first",
      },
      conversation: {
        progressAriaCreate: "Skill creation progress",
        progressAriaRevise: "Skill revision progress",
        progressCreate: "Creation progress",
        progressRevise: "Revision progress",
        permissionReadOnlyCreate:
          "Your account cannot continue creating this Skill. Saved session content and candidate files remain available to view.",
        permissionReadOnlyRevise:
          "Your account cannot continue revising this Skill. Saved session content and candidate files remain available to view.",
        reviseIntroBefore: "Loaded",
        reviseIntroAfter:
          "from the Current Version. Describe what to change, or edit the candidate files on the right.",
        createIntroBefore: "The new Skill is named",
        createIntroAfter:
          ". Describe its purpose, triggers, inputs and outputs, and any references or scripts it needs.",
        creatingSkill: "Creating Skill…",
        creatingDraft: "Creating a Candidate Version…",
        processing: "The Builder Agent is working…",
        composerAriaCreate: "Describe the Skill you want",
        composerAriaRevise: "Describe what to change",
        saveLocalChangesFirst:
          "Save or discard the file changes on the right first",
        answerQuestionFirst: "Answer the question above first",
        generatingFiles: "Generating candidate files…",
        placeholderCreate:
          "Keep describing the Skill, or ask to adjust the candidate files.",
        placeholderRevise:
          "Keep describing what to change, or ask to adjust the candidate files.",
        send: "Send",
        fallbackTitle: "Create Skill",
        revisingBanner: (slug, version) => `Revising ${slug} v${version}`,
        unsavedChanges: "Unsaved changes",
        agentRunning: "The Builder Agent is running",
        checkedCreate: "Checked; ready to create",
        checkedRevise: "Checked; ready to save a Candidate Version",
        autosave: "Automatically saved; continue later",
        more: "More actions",
        abandonCreate: "Abandon this creation",
        abandonRevise: "Abandon this revision",
        conversationAria: "Skill creation conversation",
        workbenchAria: "Skill workbench",
        sessionUnavailable: "The Skill design session is unavailable.",
        retrying: "Retrying…",
        retry: "Retry",
        backToSkills: "Back to Skills",
        continueLater: "Continue later and return to Skills",
      },
      workbench: {
        packageAria: "Candidate files",
        title: "Candidate files",
        titleRevise: "Candidate files (revision)",
        filesSurface: "Files",
        secretsSurface: "Runtime credentials",
        secretsUnavailable:
          "The candidate package has no root SKILL.md, so environment variable declarations cannot be edited.",
        fileCount: (count) => `${count} UTF-8 text files`,
        diffSummary: (version, added, modified, deleted) =>
          ` · vs baseline${version}: ${added} added · ${modified} modified · ${deleted} deleted`,
        updating: "Updating",
        readOnly: "Read-only",
        closeAria: "Close candidate files",
        empty:
          "Candidate files appear here after you describe the Skill in the conversation.",
        deletedFromBase: "Removed from baseline:",
        displayModeAria: "File display mode",
        source: "Source",
        preview: "Preview",
        editFile: (path) => `Edit ${path}`,
        selectFile: "Select a file to view its contents.",
        baselineStale:
          "The candidate package was updated elsewhere. Local edits can still be copied; load the latest version before editing.",
        unsavedHint:
          "The file has unsaved changes. Save before continuing the conversation or running checks.",
        loadLatest: "Load latest version",
        discard: "Discard changes",
        saving: "Saving…",
        save: "Save changes",
        checkPassed: "Checks passed",
        checkPassedWithWarnings: "Checks passed with warnings",
        requiredCredentials: "Required credentials:",
        recheckHint:
          "After every candidate-file change, paths, frontmatter, security rules, and quota must be checked again.",
        acknowledgeWarnings: "I understand and accept the warnings above",
        checkSkill: "Check Skill",
        commitCreate: "Create Skill (disabled by default)",
        commitRevise: "Save Candidate Version",
      },
      activity: {
        run: {
          pending: "Queued, waiting to run",
          running: "Running",
          success: "This turn completed",
          error: "This turn failed",
          timeout: "This turn timed out",
          interrupted: "This turn was interrupted",
          cancelled: "This turn was cancelled",
        },
        tool: {
          pending: "Waiting to call",
          running: "Calling",
          completed: "Completed",
          failed: "Call failed",
        },
        toolSteps: (count) => `· ${count} tool steps`,
        noToolSteps: "· No tool steps yet",
        outputLimit:
          "This turn reached the model output limit. Candidate files that were written successfully are kept. Send “continue from the existing candidate files” below; the Builder will reread them and will not execute incomplete tool calls.",
      },
      composer: {
        mode: {
          flash: "Flash",
          thinking: "Thinking",
          pro: "Pro",
          ultra: "Ultra",
        },
        modeDescription: {
          flash: "Turn off extended thinking for faster generation",
          thinking: "Turn on extended thinking at low reasoning intensity",
          pro: "Medium reasoning intensity; balances quality and time",
          ultra: "High reasoning intensity; better for complex Skills",
        },
        removeAttachment: (name) => `Remove attachment ${name}`,
        addReference: "Add reference files",
        selectModel: "Select model",
        defaultModel: "Default model",
        defaultBadge: "Default",
        selectThinking: "Select thinking intensity",
      },
      files: {
        tooltip: "View candidate files",
        aria: "View candidate files",
        label: "Files",
      },
      dialogs: {
        commitTitleCreate: "Create Skill?",
        commitTitleRevise: "Save Candidate Version?",
        commitDescriptionCreate: (project) =>
          `This saves immutable Candidate Version v1 in ${project}. The Skill stays disabled and is not added to any Agent automatically.`,
        commitDescriptionRevise: (slug, version) =>
          `This saves a new Candidate Version based on ${slug} v${version}. Activation makes it the Current Version; the running Skill is unchanged by saving.`,
        fileMetaCreate: (count) => `${count} files · disabled by default`,
        fileMetaRevise: (count) => `${count} files · Candidate Version`,
        backToReview: "Back to review",
        creating: "Creating…",
        creatingVersion: "Creating new version…",
        confirmCreate: "Create",
        confirmCreateVersion: "Create new version",
        staleTitle: "The Current Version changed",
        staleDescription: (version) =>
          `This revision is based on v${version}, but the Current Version changed. Abandon this session and start a new one from the Current Version.`,
        confirmOverwrite: "Back to Skills",
        abandonTitleCreate: "Abandon this Skill creation?",
        abandonTitleRevise: "Abandon this revision?",
        abandonDescriptionCreate:
          "This design session will end, the candidate files will be cleaned up, and it will leave the unfinished list.",
        abandonDescriptionRevise:
          "This revision session will end and the candidate files will be cleaned up. Saved Skill versions are not affected.",
        continueCreate: "Continue creating",
        continueRevise: "Continue revising",
        abandoning: "Abandoning…",
        confirmAbandon: "Abandon",
        discardTitle: "Discard unsaved changes?",
        discardDescription:
          "The Skill Builder session remains available, but these file edits will not be saved.",
        continueEditing: "Continue editing",
        discardAndLeave: "Discard changes and leave",
      },
      success: {
        withVersion: (version) =>
          `Saved Candidate Version v${version}. Go activate it`,
        withoutVersion: "Saved a Candidate Version. Go activate it",
        goPublish: "Go activate",
        revisionWithSecrets: (version, count) =>
          `Saved ${version === null ? "a Candidate Version" : `Candidate Version v${version}`} with ${count} environment variable declaration${count === 1 ? "" : "s"}. Configure runtime credentials before activation.`,
        createdWithSecrets: (count) =>
          `Skill created and suspended with ${count} environment variable declaration${count === 1 ? "" : "s"}. Configure runtime credentials before enabling it.`,
        configureCredentials: "Configure credentials",
      },
      publish: {
        staleTitle: "The Current Version changed",
        staleNamed: (live, base) =>
          `The Current Version is v${live}. This revision is based on v${base} and cannot be saved or activated. Start a new revision from v${live}.`,
        staleGeneric:
          "This version is not a forward descendant of the Current Version and cannot be saved or activated.",
      },
    },
  },

  // Breadcrumb
  breadcrumb: {
    workspace: "Workspace",
    chats: "Chats",
  },

  // Workspace
  workspace: {
    officialWebsite: "ActWeave's official website",
    settingsAndMore: "Settings and more",
    contactUs: "Contact us",
    about: "About ActWeave",
    logout: "Log out",
    gatewayUnavailable: "Gateway is temporarily unavailable.",
    gatewayUnavailableRetrying: "Retrying in the background…",
  },

  projectWorkspace: {
    title: "Workspace",
    subtitle: "Manage and enter your projects",
    account: "Account",
    platformAdministration: "Platform administration",
    systemSettings: "System settings",
    privacyCenter: "Privacy center",
    logout: "Log out",
    searchProjects: "Search projects",
    searchPlaceholder: "Search by name or project slug",
    filterProjects: "Filter projects",
    allProjects: "All projects",
    pinnedOnly: "Pinned only",
    projectCount: (count) => `${count} ${count === 1 ? "project" : "projects"}`,
    createProject: "Create project",
    projectList: "Project list",
    projectLoadFailed: "Projects could not be loaded",
    loadingProjects: "Loading projects",
    retry: "Retry",
    columns: {
      project: "Project",
      description: "Description",
      actions: "Actions",
    },
    card: {
      edit: "Edit project",
      editAction: "Edit",
      pin: "Pin project",
      pinAction: "Pin",
      unpin: "Unpin project",
      pinned: "Pinned",
      noDescription: "No project description",
      open: "Open project",
    },
    empty: {
      noMatchesTitle: "No matching projects",
      firstProjectTitle: "Create your first project",
      noMatchesDescription:
        "Try another keyword, or clear the filters to view all projects.",
      firstProjectDescription:
        "Projects organize members and shared Agents, Skills, and MCPs.",
      clearFilters: "Clear filters",
    },
    createDialog: {
      title: "Create project",
      description:
        "You will become the project Admin and can then invite members and configure shared assets.",
      projectName: "Project name",
      projectSlug: "Project slug",
      descriptionLabel: "Description",
      slugHelp:
        "Use 3–63 lowercase letters, numbers, or hyphens. A hyphen cannot appear first or last or be repeated.",
      slugRequired: "Enter a project slug.",
      slugTooShort: "The project slug must have at least 3 characters.",
      slugTooLong: "The project slug cannot exceed 63 characters.",
      slugInvalid:
        "The project slug can contain only lowercase letters, numbers, and single hyphens, and cannot start or end with a hyphen.",
      cancel: "Cancel",
      creating: "Creating…",
      create: "Create project",
    },
    editDialog: {
      title: "Edit project",
      slugImmutable: "The project slug cannot be changed.",
      projectName: "Project name",
      descriptionLabel: "Description",
      saving: "Saving…",
      save: "Save changes",
    },
    recovery: {
      title: "Recoverable projects",
      windowEnd: "recovery window end",
      recoverableUntil: (deadline) => `Recoverable until ${deadline}`,
      recover: "Recover project",
      confirmTitle: "Recover this project?",
      confirmDescription: (projectName) =>
        `Member access and frozen private work in “${projectName}” will be restored. Automations remain paused after recovery.`,
      cancel: "Cancel",
      restoring: "Restoring…",
      confirm: "Recover",
      empty: "No projects are currently recoverable.",
    },
    notifications: {
      trigger: "Notifications",
      unreadTrigger: (count) =>
        `Notifications, ${count} unread ${count === 1 ? "item" : "items"}`,
      title: "Notifications",
      description: "Review system messages and respond to project invitations.",
      loading: "Loading notifications…",
      empty: "No notifications",
      retry: "Retry",
      loadingMore: "Loading…",
      loadMore: "Load more",
      readSyncPending: "Some notification read states have not synced yet.",
      operationFailed: "The notification action failed. Try again later.",
      invitationTitle: "Project invitation",
      invitedBy: (actor, projectName) =>
        `${actor} invited you to join ${projectName}`,
      role: (role) => `Role: ${role}`,
      accepting: "Joining…",
      accept: "Join project",
      joined: "Joined project",
      statuses: {
        pending: "Pending",
        redeemed: "Joined",
        revoked: "Revoked",
        expired: "Expired",
      },
      roles: {
        editor: "Editor",
        runner: "Runner",
        viewer: "Viewer",
      },
    },
    errors: {
      slugConflict: "That project slug already exists. Choose another one.",
      unavailable:
        "The project is unavailable or your membership has expired. Return to the workspace.",
      lastAdmin:
        "The last Admin cannot be removed or downgraded. Assign another Admin first.",
      memberQuotaExceeded:
        "This project has reached its member limit. Ask a project administrator to increase the limit, then reopen the invitation link.",
      membershipVersionConflict:
        "Member information changed. Refresh and try again.",
      quotaStateConflict:
        "The member quota state is inconsistent. Refresh and try again; contact an administrator if the problem continues.",
      invitationConflict:
        "This invitation already exists or was just handled. Refresh and try again.",
      invitationInvalid:
        "This invitation is expired, revoked, or does not apply to this account.",
      deletionStateConflict:
        "The project state changed. Refresh the workspace and try again.",
      validationFailed: "Check the project information and try again.",
      authRequired: "Your sign-in has expired. Sign in again.",
      serviceUnavailable:
        "The project service is temporarily unavailable. Try again later.",
      requestFailed: "The project request failed. Try again later.",
    },
  },

  // Conversation
  conversation: {
    noMessages: "No messages yet",
    startConversation: "Start a conversation to see messages here",
    branchCreated: "Conversation branch created",
    branchFailed: "Failed to branch conversation.",
    runFailedTitle: "Run did not finish",
    runFailedDescription:
      "The agent could not produce a response. Check the selected model, asset dependencies, and credentials, then edit or send the message again.",
    providerUnavailableTitle: "Model provider temporarily unavailable",
    providerUnavailableDescription:
      "The Worker could not reach the configured model provider after retrying. Check the Worker network or proxy configuration, then retry this message.",
    runAdmissionNotConfirmedDescription:
      "The Run did not start because the conversation may already be running or its state changed. Your input was preserved; try again shortly.",
    restoreFailedInput: "Restore to composer",
    restoreFailedInputBlocked:
      "The composer already has unsent content or attachments. Clear it before restoring this message.",
    modelOutputLimitTitle: "Model output limit reached",
    modelOutputLimitDescription:
      "The model reached its per-request output limit, so this response is incomplete.",
    modelOutputLimitRetry: "Retry without deep thinking",
    modelOutputLimitRetrying: "Retrying…",
    tokenBudgetReachedTitle: "Run token budget reached",
    tokenBudgetReachedDescription:
      "This run stopped early, so the response may be incomplete.",
    outputDeliveryIncompleteTitle: "Output file was not delivered",
    outputDeliveryIncompleteDescription:
      "A required output file was created but was not published to this conversation. Resending may repeat an already completed command, so review the run first.",
    currentUploadUnavailableTitle: "Image attachment could not be read",
    currentUploadUnavailableDescription:
      "This Run could not securely read or validate the current image attachment. Restore the original input and retry; if it still fails, remove and paste the image again.",
    agentSuspendedTitle: "Agent suspended",
    agentSuspendedDescription:
      "The project Agent bound to this chat is suspended, so new messages cannot be sent. Ask a project administrator to reactivate it or start a new chat with another Agent.",
    agentArchivedTitle: "Agent deleted",
    agentArchivedDescription:
      "This Agent was deleted, so this chat cannot continue. Choose another Agent to start a new chat.",
    agentArchivedAction: "Choose another Agent for a new chat",
    agentModelUnavailableTitle: "Agent model unavailable",
    agentModelUnavailableDescription:
      "The Agent's configured model could not be resolved. Check its system binding, Current Version, and active model catalog entry, then retry.",
    runExecutionProfile: (modelDisplayName, modeName, supportsVision) =>
      `Effective run: ${modelDisplayName} · ${modeName} · ${supportsVision ? "vision-capable" : "text only"}`,
  },

  // Chats
  chats: {
    searchChats: "Search chats",
    loadMoreToSearch: "Load more to search older conversations",
    loadingMore: "Loading more...",
    loadOlderChats: "Load older chats",
  },

  // Sidecar
  sidecar: {
    title: "Side chat",
    open: "Open side chat",
    close: "Close side chat",
    delete: "Delete side chat",
    deleteConfirm:
      "Are you sure you want to delete this side chat? If it is still running, deletion stops the run immediately; external actions already sent may not be reversible. This action cannot be undone. To simply hide it, use the side chat toggle in the header instead.",
    deleteSuccess: "Side chat deleted",
    deleteFailed: "Failed to delete side chat.",
    addToConversation: "Add to conversation",
    askInSideChat: "Ask in side chat",
    reference: "Reference",
    selectedTextFragment: "{count} selected text fragment",
    selectedTextFragments: "{count} selected text fragments",
    clearReferences: "Clear selected references",
    emptyTitle: "Ask a follow-up",
    emptyDescription: "Ask a follow-up grounded in the referenced text.",
    placeholder: "Ask a deeper follow-up...",
    send: "Send",
    sendFailed: "Failed to send side chat message.",
    noContext: "No context selected",
    continuing: "Continue in this side chat",
    selectionCrossesMessages:
      "Selection spans multiple messages. Select text within a single reply to quote it.",
  },

  // Channels
  channels: {
    title: "Channels",
    connect: "Connect",
    modify: "Modify",
    reconnect: "Reconnect",
    disconnect: "Disconnect",
    connected: "Connected",
    notConnected: "Not connected",
    pending: "Pending",
    revoked: "Disconnected",
    disabled: "Disabled",
    unconfigured: "Not configured",
    unavailable: "Channel connections are unavailable right now.",
    unavailableShort: "Unavailable",
    setupTitle: (name: string) => `Connect ${name}`,
    setupEditTitle: (name: string) => `Modify ${name}`,
    setupDescription:
      "Enter the values needed by this server process. They are not written to config.yaml.",
    saveAndConnect: "Save and connect",
    saveChanges: "Save changes",
    descriptions: {
      telegram: "Telegram direct messages through your ActWeave bot.",
      slack: "Slack workspace messages and mentions.",
      discord: "Discord server messages through your ActWeave bot.",
      feishu: "Feishu and Lark messages through your ActWeave app.",
      dingtalk: "DingTalk Stream Push messages through your ActWeave bot.",
      wechat: "WeChat iLink messages through your ActWeave bot.",
      wecom: "WeCom messages through your ActWeave AI bot.",
    },
    connectedAs: (name: string) => `Connected as ${name}.`,
  },

  // Page titles (document title)
  pages: {
    appName: "ActWeave",
    chats: "Chats",
    newChat: "New chat",
    untitled: "Untitled",
  },

  // Tool calls
  toolCalls: {
    moreSteps: (count: number) => `${count} more step${count === 1 ? "" : "s"}`,
    lessSteps: "Less steps",
    executionDetails: "Execution details",
    stepCount: (count: number) => `${count} ${count === 1 ? "step" : "steps"}`,
    executeCommand: "Execute command",
    presentFiles: "Present files",
    needYourHelp: "Need your help",
    useTool: (toolName: string) => `Use "${toolName}" tool`,
    searchFor: (query: string) => `Search for "${query}"`,
    searchForRelatedInfo: "Search for related information",
    searchForRelatedImages: "Search for related images",
    searchForRelatedImagesFor: (query: string) =>
      `Search for related images for "${query}"`,
    searchOnWebFor: (query: string) => `Search on the web for "${query}"`,
    viewWebPage: "View web page",
    listFolder: "List folder",
    readFile: "Read file",
    writeFile: "Write file",
    clickToViewContent: "Click to view file content",
    writeTodos: "Update to-do list",
    rememberMemory: "Save to memory",
    remembered: "Remembered:",
    memoryDisabledNotSaved: "Memory is disabled; not saved",
    skillInstallTooltip: "Install skill and make it available to ActWeave",
  },

  humanInput: {
    answered: "Answered",
    pending: "Sending...",
    readOnly: "Read only",
    attentionCount: (count: number) =>
      `${count} item${count === 1 ? "" : "s"} needs attention`,
    changeBeforeSubmit: "You can change your selection before submitting.",
    otherLabel: "Other answer",
    otherPlaceholder: "Type another answer...",
    submit: "Submit answer",
    emptyError: "Enter an answer before submitting.",
    requiredError: "Fill in all required fields before submitting.",
    requiredA11yLabel: "required",
    selectPlaceholder: "Select...",
    availableOptions: "Available options",
    requestedInformation: "Requested information",
    selected: "Selected",
    yourAnswer: "Your answer",
    answeredValue: (value: string) => `Answered: ${value}`,
  },

  executionApproval: {
    title: "Request to run a command in the ActWeave Worker host environment.",
    localHost: "Local host",
    riskTitle: "Runs with your local account permissions",
    riskWarning:
      "This is not an isolated sandbox. The command runs in the ActWeave Worker OS environment and may access files and credentials available to your local account, use the network, start background processes, and return output to the Agent or model. Allow it only if you trust this exact command.",
    command: "Exact command",
    workingDirectory: "Working directory",
    sourceAgent: "Source Agent",
    effectiveUser: "Local account",
    timeout: "Timeout",
    timeoutSeconds: (seconds) => `${seconds} seconds`,
    expiresIn: (seconds) => `This request expires in ${seconds} seconds.`,
    allowOnce: "Allow once",
    deny: "Deny",
    allowing: "Allowing…",
    denying: "Denying…",
    decisionFailed: "The decision could not be submitted. Please try again.",
    exitCode: (code) => `Exit code: ${code}`,
    reason: "Reason",
    finishedWarning:
      "The main process exited, but this does not prove that it had no side effects or left no background processes.",
    unknownTitle: "Execution state is unknown",
    unknownWarning:
      "The command or one of its child processes may still be running and producing side effects. Inspect the ActWeave Worker host environment before requesting it again; this authorization cannot be retried.",
    statuses: {
      pending: "Waiting for approval",
      approved: "Approved",
      claimed: "Running in the Worker host environment",
      finished: "Finished",
      launch_failed: "Launch failed",
      unknown: "State unknown",
      denied: "Denied",
      expired: "Expired",
      cancelled: "Cancelled",
    },
  },

  // Subtasks
  uploads: {
    uploading: "Uploading...",
    uploadingFiles: "Uploading files, please wait...",
    ready: "Ready to send",
    limitsHint: (maxFiles: number, maxFileSize: string, maxTotalSize: string) =>
      `Add attachments (up to ${maxFiles} files, ${maxFileSize} each, ${maxTotalSize} total). Most regular file types are supported; compress macOS .app bundles first.`,
    filesTooLarge: (files: string, maxFileSize: string) =>
      `Files exceeding the ${maxFileSize} per-file limit were not added: ${files}.`,
    tooManyFiles: (count: number, maxFiles: number) =>
      `${count} file${count === 1 ? " was" : "s were"} not added. You can attach up to ${maxFiles} files at once.`,
    totalSizeTooLarge: (count: number, maxTotalSize: string) =>
      `${count} file${count === 1 ? " was" : "s were"} not added. Attachments can total up to ${maxTotalSize}.`,
    projectStorageTooSmall: (count: number, remainingSize: string) =>
      `${count} file${count === 1 ? " was" : "s were"} not added. The project currently has ${remainingSize} of storage remaining.`,
    serverTooLarge:
      "The server rejected files that exceed the upload limits. Adjust them and try again.",
    storageQuotaExceeded:
      "Project storage was consumed or exhausted by another operation. Delete files or contact a project administrator.",
    preflightRejected:
      "Upload preflight failed. Adjust the files to match the attachment limits.",
    uploadFailed: "File upload failed. Please try again.",
    cleanupFailed:
      "An unused uploaded file could not be removed. Refresh the file list and try again.",
  },

  subtasks: {
    subtask: "Subtask",
    executing: (count: number) =>
      `Executing ${count === 1 ? "" : count + " "}subtask${count === 1 ? "" : "s in parallel"}`,
    in_progress: "Running subtask",
    completed: "Subtask completed",
    failed: "Subtask failed",
  },

  // Token Usage
  tokenUsage: {
    title: "Token Usage",
    label: "Tokens",
    input: "Input",
    output: "Output",
    total: "Total",
    view: "Display",
    unavailable:
      "No token usage yet. Usage appears only after a successful model response when the provider returns usage_metadata.",
    unavailableShort: "No usage returned",
    note: "Header totals use persisted thread usage, plus visible in-flight usage while a run is still streaming. Per-turn and debug usage come from currently visible messages only, so their scopes may differ.",
    presets: {
      off: "Off",
      summary: "Summary",
      perTurn: "Per turn",
      debug: "Debug",
    },
    presetDescriptions: {
      off: "Hide token usage in the header and conversation.",
      summary: "Show only the current conversation total in the header.",
      perTurn:
        "Show the header total and one token summary per assistant turn.",
      debug: "Show the header total and step-level token debugging details.",
    },
    finalAnswer: "Final answer",
    stepTotal: "Step total",
    sharedAttribution: "Shared across multiple actions in this step",
    subagent: (description: string) => `Subagent: ${description}`,
    startTodo: (content: string) => `Start To-do: ${content}`,
    completeTodo: (content: string) => `Complete To-do: ${content}`,
    updateTodo: (content: string) => `Update To-do: ${content}`,
    removeTodo: (content: string) => `Remove To-do: ${content}`,
  },

  contextWindow: {
    title: "Context window",
    automaticCompression: "Automatic compression progress",
    loading: "Measuring the current context…",
    unavailable: "Current context usage is unavailable.",
    disabled: "Automatic context compression is disabled.",
    progressLabel: (percent: string) =>
      `${percent} of the automatic compression threshold reached`,
    current: "Current",
    triggerAt: "Compress at",
    remaining: "Remaining",
    estimatedContext: "Estimated context",
    tokenThreshold: "Token threshold",
    summaryPresent: "Includes the previous compression summary",
    allConditions: "Configured conditions",
    anyCondition: "Compression starts when any condition is reached.",
    primary: "Closest",
    triggerTypes: {
      tokens: "Token condition",
      fraction: "Percentage condition",
      messages: "Message condition",
    },
    tokens: (value: string) => `${value} Tokens`,
    tokenPair: (current: string, total: string) =>
      `${current} / ${total} Tokens`,
    messages: (count: number) =>
      `${count.toLocaleString("en-US")} message${count === 1 ? "" : "s"}`,
  },

  // Shortcuts
  shortcuts: {
    searchActions: "Search actions...",
    noResults: "No results found.",
    actions: "Actions",
    keyboardShortcuts: "Keyboard Shortcuts",
    keyboardShortcutsDescription:
      "Navigate ActWeave faster with keyboard shortcuts.",
    openCommandPalette: "Open Command Palette",
    toggleSidebar: "Toggle Sidebar",
  },

  // Settings
  settings: {
    title: "Settings",
    description: "Adjust how ActWeave looks and behaves for you.",
    sections: {
      account: "Account",
      personalization: "Personalization",
      appearance: "Appearance",
      channels: "Channels",
      memory: "Memory",
      tools: "Tools",
      skills: "Skills",
    },
    memory: {
      title: "Memory",
    },
    personalization: {
      title: "Memory",
      description:
        "Control how ActWeave collects, retains, and uses long-term memory for your account.",
      loading: "Loading Memory settings…",
      loadError: "Memory settings could not be loaded",
      loadErrorDescription:
        "Your current setting has not changed. Please try again.",
      retry: "Retry",
      enableTitle: "Enable Memory",
      enableDescription:
        "Create long-term memory from chats and recall it in future conversations.",
      platformUnavailable:
        "Memory is currently disabled by the system. Your preference is still saved.",
      saving: "Saving…",
      enableSuccess: "Memory enabled",
      disableSuccess: "Memory disabled; existing content is preserved",
      updateError: "Memory settings could not be updated. Please try again.",
      conflict:
        "This setting changed elsewhere. It has been refreshed; please try again.",
      resetTitle: "Reset Memory",
      resetDescription:
        "Delete long-term memory, pending content, and archived episodes for this account in every project.",
      resetButton: "Reset",
      resetDialogTitle: "Reset all Memory?",
      resetDialogDescription:
        "This permanently deletes your long-term document, pending history, archived episodes, versions, Dream records, and Memory snapshots. It cannot be undone.",
      resetChatNotice:
        "Chats, thread context, files, and /compact summaries will not be deleted.",
      cancel: "Cancel",
      confirmReset: "Reset Memory",
      resetting: "Resetting…",
      resetSuccess: "Memory reset; chats were left unchanged",
      resetError: "Memory could not be reset. Please try again.",
    },
    appearance: {
      themeTitle: "Theme",
      themeDescription:
        "Choose how the interface follows your device or stays fixed.",
      system: "System",
      light: "Light",
      dark: "Dark",
      systemDescription: "Match the operating system preference automatically.",
      lightDescription: "Bright palette with higher contrast for daytime.",
      darkDescription: "Dim palette that reduces glare for focus.",
      chatWidthTitle: "Chat content width",
      chatWidthDescription:
        "Adjust the maximum width of messages and the composer to control the space on both sides.",
      chatWidthNarrow: "Focused",
      chatWidthStandard: "Standard",
      chatWidthWide: "Wide",
      chatWidthFull: "Full width",
      languageTitle: "Language",
      languageDescription: "Switch between languages.",
    },
    tools: {
      title: "Tools",
      description: "Manage the configuration and enabled status of MCP tools.",
      adminRequired: "Admin privileges are required to manage MCP tools.",
      empty: "No MCP tools configured.",
    },
    channels: {
      title: "Channels",
      description:
        "Connect IM accounts that can send messages to ActWeave from outside the browser.",
      disabled:
        "Channel connections are not enabled on this server. Ask an administrator to enable channel_connections.",
    },
    skills: {
      title: "Agent Skills",
      description:
        "Manage the configuration and enabled status of the agent skills.",
      createSkill: "Create skill",
      emptyTitle: "No agent skill yet",
      emptyDescription:
        "Put your agent skill folders under the `/skills/custom` folder under the root folder of ActWeave.",
      emptyButton: "Create Your First Skill",
      adminRequired: "Admin privileges are required to manage agent skills.",
      installAdminRequired:
        "Admin privileges are required to install agent skills.",
      viewSkill: (name) => `View ${name} SKILL.md`,
      toggleSkill: (name) => `Enable or disable ${name}`,
      fileLabel: "SKILL.md",
      renderedDescription: "Rendered contents of SKILL.md",
      enabled: "Enabled",
      disabled: "Disabled",
      categories: { public: "Public", custom: "Custom", legacy: "Legacy" },
      adminRequiredPreview:
        "Admin privileges are required to preview skill content.",
      contentUnavailable: "Skill content is unavailable.",
      loadError: "Unable to load skill content.",
      emptyContent: "This SKILL.md is empty.",
      licenseLabel: "License",
      loading: "Loading skill content",
      retry: "Retry",
    },
    account: {
      profileTitle: "Profile",
      email: "Email",
      username: "Username",
      role: "Role",
      roles: {
        system_admin: "System admin",
        user: "User",
      },
      ssoProvider: "SSO",
      changePasswordTitle: "Change Password",
      changePasswordDescription: "Update your account password.",
      ssoPasswordDescription: "Password is managed by your SSO provider.",
      ssoPasswordMessage:
        "This account signs in with {provider}, so ActWeave cannot manage or change its password here. Use your SSO provider's account settings instead.",
      currentPassword: "Current password",
      newPassword: "New password",
      confirmNewPassword: "Confirm new password",
      passwordMismatch: "New passwords do not match",
      passwordTooShort: "Password must be at least 8 characters",
      passwordChangedSuccess: "Password changed successfully",
      networkError: "Network error. Please try again.",
      updating: "Updating...",
      updatePassword: "Update Password",
      signOut: "Sign Out",
    },
    acknowledge: {
      emptyTitle: "Acknowledgements",
      emptyDescription: "Credits and acknowledgements will show here.",
    },
  },
  login: {
    signInTitle: "Sign in to your account",
    createAccountTitle: "Create a new account",
    identifier: "Email or username",
    identifierPlaceholder: "Email or username",
    username: "Username",
    usernamePlaceholder: "Start with a letter; 3–32 letters, digits, or _",
    usernameHint:
      "Letters, digits, and underscore only. No Chinese or special characters.",
    usernameInvalid:
      "Username must be 3–32 characters, start with a letter, and use only letters, digits, or underscore.",
    usernameTaken: "This username is already taken.",
    email: "Email",
    emailPlaceholder: "you@example.com",
    emailTaken: "This email is already registered.",
    password: "Password",
    passwordPlaceholder: "•••••••",
    rememberMe: "Remember this session and account",
    checkingRegistration: "Checking account registration status…",
    registrationUnavailable:
      "Account registration status is temporarily unavailable.",
    registrationDisabled:
      "Regular account registration is disabled by an administrator.",
    retry: "Retry",
    pleaseWait: "Please wait...",
    signIn: "Sign In",
    createAccount: "Create Account",
    createAdminAccount: "Create admin account",
    adminSetupRequiredTitle: "Administrator setup is required",
    adminSetupRequiredDescription:
      "ActWeave needs an administrator account before new regular accounts can be created.",
    orContinueWith: "Or continue with",
    ssoHint:
      "If your account uses single sign-on, sign in with the option below instead.",
    continueWith: (provider: string) => `Continue with ${provider}`,
    noAccountSignUp: "Don't have an account? Sign up",
    haveAccountSignIn: "Already have an account? Sign in",
    backToHome: "← Back to home",
    networkError: "Network error. Please try again.",
    authFailed: "Authentication failed.",
    errors: {
      sso_failed: "SSO login failed. Please try again or use email login.",
      sso_cancelled: "SSO login was cancelled.",
      sso_account_exists:
        "An account with this email already exists. Please sign in with your password or contact your administrator.",
      sso_not_allowed:
        "SSO login is not allowed for your account. Contact your administrator.",
    },
  },
  setup: {
    loading: "Loading…",
    initAdminTitle: "Create admin account",
    initAdminDescription: "Set up the administrator account to get started.",
    username: "Username",
    usernamePlaceholder: "Start with a letter; 3–32 letters, digits, or _",
    usernameHint:
      "Letters, digits, and underscore only. No Chinese or special characters.",
    usernameInvalid:
      "Username must be 3–32 characters, start with a letter, and use only letters, digits, or underscore.",
    email: "Email",
    emailPlaceholder: "you@example.com",
    password: "Password",
    passwordPlaceholder: "Password (min. 8 characters)",
    confirmPassword: "Confirm Password",
    confirmPasswordPlaceholder: "Confirm password",
    passwordMismatch: "Passwords do not match",
    passwordTooShort: "Password must be at least 8 characters",
    networkError: "Network error. Please try again.",
    creatingAccount: "Creating account…",
    createAdminAccount: "Create Admin Account",
    completeAdminTitle: "Complete admin account setup",
    completeAdminDescription: "Set your real email and a new password.",
    yourEmailPlaceholder: "Your email",
    currentPassword: "Current password",
    newPassword: "New password",
    confirmNewPassword: "Confirm new password",
    settingUp: "Setting up…",
    completeSetup: "Complete Setup",
  },
};
