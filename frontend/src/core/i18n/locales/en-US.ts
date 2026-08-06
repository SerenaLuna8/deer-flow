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
    blog: "Blog",
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
        loading: "Loading usage",
        unavailableTitle: "Usage is unavailable",
        unavailableDescription:
          "The project usage service could not be read safely.",
        thresholdReached: "80% threshold reached",
        used: "Used",
        reserved: "Reserved",
        limit: "Limit",
        tightenTitle: "Tighten project limits",
        updateError: "Limits were not updated. Refresh and retry.",
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
      },
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
      previousPage: "Newer",
      page: (page) => `Page ${page}`,
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
      audit: "Audit",
      assets: "Assets",
      systemSettings: "System settings",
      settings: "Model settings",
    },
    overview: {
      title: "Operations overview",
      description: "Current readiness, workload, and aggregate quota usage.",
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
          audit: "Audit",
        },
      },
      channels: {
        title: "Channel providers",
        emptyTitle: "No provider health reports",
        empty: "No channel providers are configured or reporting health.",
        checkedAt: "Checked {time}",
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
      description: "Public operational state for every project.",
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
      description: "Public job state and explicitly safe recovery actions.",
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
      },
      retrySafety: {
        safe: "Safe to retry",
        unsafe: "Unsafe to retry",
        unknown: "Unknown",
      },
      filters: {
        label: "Filter jobs",
        project: "Project",
        projectQuery: "Search projects",
        projectQueryPlaceholder: "Search by project name or slug",
        status: "Status",
        type: "Job type",
        allStatuses: "All statuses",
        allTypes: "All job types",
        apply: "Search",
        clear: "Reset",
        invalidQuery: "Search text must be 120 characters or fewer.",
      },
    },
    audit: {
      title: "Audit",
      description: "Allowlisted platform governance events.",
      loading: "Loading audit events",
      unavailableTitle: "Operations data is unavailable",
      unavailableDescription: "Audit events could not be loaded.",
      emptyTitle: "No audit events found",
      emptyDescription: "No platform audit events are available.",
      older: "Older events",
    },
  },

  adminModelSettings: {
    header: {
      eyebrow: "System settings",
      title: "Model settings",
      description:
        "Manage available platform models, the default model, and system Credential references. Credential values are never read or displayed here.",
      create: "Create model",
    },
    overview: {
      label: "Model catalog overview",
      configured: "Configured models",
      configuredDetail: "Models in the platform catalog",
      active: "Active models",
      activeDetail: "Available for conversations",
      defaultModel: "Default model",
      defaultDetail: "System default for new conversations",
      notSet: "Not set",
      revision: "Catalog revision",
      revisionDetail: "Used for concurrency checks",
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
      catalogDescription:
        "Each entry shows only non-sensitive configuration metadata and exact Credential references.",
      modelCount: (count) => `${count} ${count === 1 ? "model" : "models"}`,
    },
    card: {
      defaultModel: "Default model",
      active: "Enabled",
      suspended: "Suspended",
      updatedAt: (formattedDate) => `Updated ${formattedDate}`,
      updatedAtColumn: "Updated",
      providerModel: "Provider model",
      credential: "Credential",
      environmentKey: "Environment variable",
      status: "Status",
      version: "Version",
      versionMeta: (versionNumber, revision, sortOrder) =>
        `Config v${versionNumber} · revision ${revision} · order ${sortOrder}`,
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
      basicDescription:
        "Define the stable logical name, Provider mapping, and catalog details.",
      logicalName: "Logical name",
      displayName: "Display name",
      displayNamePlaceholder: "Analysis Pro",
      providerAdapter: "Provider Adapter",
      providerModel: "Provider model",
      status: "Status",
      active: "Enable",
      suspended: "Suspend",
      modelDescription: "Description",
      modelDescriptionPlaceholder: "Recommended use cases",
      sortOrder: "Sort order",
      sortOrderHint: "Lower values appear first. New models use 0 by default.",
      capabilities: "Model capabilities",
      capabilitiesAndRuntime: "Capabilities and runtime parameters",
      capabilitiesDescription:
        "These capabilities determine which input and reasoning controls are available in conversations.",
      supportsThinking: "Supports thinking",
      supportsReasoningEffort: "Supports reasoning effort",
      supportsVision: "Supports vision input",
      commonProviderSettings: "Runtime parameters",
      commonProviderSettingsDescription:
        "Only allowlisted request parameters are saved. Secret values are never stored here.",
      baseUrl: "Base URL",
      baseUrlHint:
        "The Provider API base address. Do not include a model-specific path.",
      temperature: "Temperature",
      maxTokens: "Maximum Tokens",
      requestTimeout: "Request timeout (seconds)",
      maxRetries: "Maximum retries",
      credentialBinding: "Credential binding",
      credentialBindingDescription:
        "Bind an exact system Credential version. Secret values never enter the page cache.",
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
      sortOrder: "Sort order",
      advancedJsonInvalid: "Advanced JSON is invalid",
      advancedJsonObject: "Advanced JSON must be an object",
      advancedJsonUnsafe:
        "Advanced JSON may contain only supported safe fields with exact value types",
      invalidForm:
        "Check the required fields, model names, and Credential binding",
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
      description:
        "Set the platform's default behavior. Choose one area and save its changes independently.",
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
    },
    shell: {
      platformAria: "Platform asset management",
      systemCatalog: "System asset catalog",
      systemCatalogDescription:
        "System definitions are runtime read-only; Credential writes are controlled",
      adminScope: "Platform administrator scope",
      projectAria: "Project shared-asset governance",
      backToProjects: "Back to projects",
      projectGovernance: "Project shared-asset governance",
      projectBoundary:
        "Governs only project-owned shared Agents, Skills, MCP, and Credentials in the selected project. It never reads members, chats, runs, Memory, files, or other private user content.",
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
      credentialTypes: {
        modelApiKey: "Model API key",
        apiKey: "API key",
        token: "Access token",
        mcpAuth: "MCP authentication",
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
      migrateReferences: "Migrate compatible references",
      revokeCredential: "Revoke Credential",
      delete: "Delete",
      createCredential: "Create Credential",
      createProjectCredential: "Create project Credential",
      createProjectAsset: "Create project asset",
      retry: "Retry",
      retrying: "Retrying…",
      create: "Create",
      creating: "Creating…",
      createVersion: "Create version",
      creatingVersion: "Creating…",
      reload: "Reload",
      systemProvided: "System provided",
      projectOwned: "Project owned",
      active: "Active",
      revoked: "Revoked",
      loading: "Loading",
      migrationSuccess:
        "Compatible reference migration completed. No authorization changes were made when no eligible MCP Grant or Skill environment binding required migration.",
      credentialRotationNote:
        "Replacement creates a new version only. Existing MCP Grants and Skill environment bindings remain pinned until they are migrated explicitly.",
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
      systemEyebrow: "Platform shared assets",
      databaseCatalog: "PostgreSQL catalog",
      runtimeReadOnly: "Runtime read-only",
      controlledWrite: "Controlled secure writes",
      loading: "Loading assets",
      loadFailed: "Assets could not be loaded",
      systemCount: (count) => `${count} ${count === 1 ? "asset" : "assets"}`,
      credentialCount: (count) =>
        `${count} system ${count === 1 ? "Credential" : "Credentials"}`,
      systemNote: (kind) =>
        `System ${kind} entries are written during database initialization from the versioned, digest-verified packaged catalog and remain runtime read-only. Update the repository catalog and run the explicit database initialization flow to publish system asset changes.`,
      emptySystem: (kind) =>
        `The packaged catalog contains no system ${kind} entries.`,
      emptyCreate: "Use the create action above to add the first entry.",
      system: {
        agentsTitle: "System Agents",
        agentsDescription:
          "Governance metadata for system Agents initialized from the packaged catalog.",
        skillsTitle: "System Skills",
        skillsDescription:
          "System Skill and version metadata initialized from the packaged catalog.",
        mcpTitle: "System MCP",
        mcpDescription:
          "Governance metadata for system MCP initialized from the packaged catalog.",
        credentialsTitle: "System Credentials",
        credentialsDescription:
          "Only Credential metadata is displayed. Secret values are never returned after writing.",
      },
      projectEyebrow: "Project governance catalog",
      projectDatabaseCatalog: "PostgreSQL asset catalog",
      sharedOnly: "Shared assets only",
      projectLoadFailed: "Project assets could not be loaded",
      sourceCounts: (systemCount, projectCount) =>
        `System provided ${systemCount} · Project owned ${projectCount}`,
      project: {
        agentsTitle: "Project Agent governance",
        agentsDescription:
          "Review project-owned Agents in the selected project.",
        skillsTitle: "Project Skill governance",
        skillsDescription: "Manage complete project-owned Skill versions.",
        mcpTitle: "Project MCP governance",
        mcpDescription:
          "Manage project-owned MCP definitions, approvals, and Credential Grants.",
        credentialsTitle: "Project Credential governance",
        credentialsDescription:
          "Govern only Credential security metadata for the selected project. Secret values are never returned after writing.",
      },
    },
    catalog: {
      systemAssets: "System assets",
      systemAssetsDescription:
        "System assets are shared read-only. Project bindings pin an explicit version and never upgrade automatically.",
      searchPlaceholder: "Search by name or identifier",
      filterAll: "All statuses",
      catalogReady: "Catalog loaded",
      catalogReadyDetail: "PostgreSQL available",
      totalAssets: "Total assets",
      activeAssets: "Active assets",
      unpublishedAssets: "Unpublished assets",
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
        "Owned by this project. Agent settings are maintained on the project Agent page and take effect after saving.",
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
      createAssetTitle: (kind) => `Create ${kind}`,
      skillCreationDescription:
        "Creates a draft SKILL.md from the starter template. The Skill starts disabled.",
      assetCreationDescription: (scope) =>
        `Create the ${scope === "system" ? "system" : "project"} asset first, then create and publish a version.`,
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
      migrateTitle: "Migrate compatible Credential references",
      migrateDescription: (name) =>
        `Replacing a Credential creates a new version without rotating existing MCP Grants or Skill environment bindings. References to an older “${name}” version migrate atomically only when every field schema is compatible. Any incompatible reference rejects the entire operation.`,
      migrating: "Migrating…",
      confirmMigrate: "Migrate references",
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
    rotation: {
      title: "Credential envelope rotation",
      summary: (current, total) =>
        `${current} of ${total} active versions current`,
      current: "Rotation current",
      pending: (count) => `${count} pending`,
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
    create: "Create automation",
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
    title: "Agents",
    description:
      "Create and manage custom agents with specialized prompts and capabilities.",
    newAgent: "New Agent",
    emptyTitle: "No custom agents yet",
    emptyDescription:
      "Create your first custom agent with a specialized system prompt.",
    featureDisabledTitle: "Agents feature is not enabled",
    featureDisabledDescription:
      "This feature is not enabled on this server. Please contact your administrator.",
    chat: "Chat",
    delete: "Delete",
    deleteConfirm:
      "Are you sure you want to delete this agent? This action cannot be undone.",
    deleteSuccess: "Agent deleted",
    newChat: "New chat",
    createPageTitle: "Design your Agent",
    createPageSubtitle:
      "Describe the agent you want — I'll help you create it through conversation.",
    nameStepTitle: "Name your new Agent",
    nameStepHint:
      "Letters, digits, and hyphens only — stored lowercase (e.g. code-reviewer)",
    nameStepPlaceholder: "e.g. code-reviewer",
    nameStepContinue: "Continue",
    nameStepInvalidError:
      "Invalid name — use only letters, digits, and hyphens",
    nameStepAlreadyExistsError: "An agent with this name already exists",
    nameStepNetworkError:
      "Network request failed — check your network or backend connection",
    nameStepCheckError: "Could not verify name availability — please try again",
    nameStepCheckErrorWithDetail: "Name check failed: {detail}",
    nameStepApiDisabledError:
      "Custom agent management is not enabled on this server. Please contact your administrator.",
    nameStepBootstrapMessage:
      "The new custom agent name is {name}. Help me design its purpose, behavior, and SOUL.md before saving it.",
    save: "Save agent",
    saving: "Saving agent...",
    saveRequested:
      "Save requested. ActWeave is generating and saving an initial version now.",
    saveHint:
      "You can save this agent at any time from the top-right menu, even if this is only a first draft.",
    agentCreatedPendingRefresh:
      "The agent was created, but ActWeave could not load it yet. Please refresh this page in a moment.",
    more: "More actions",
    agentCreated: "Agent created!",
    startChatting: "Start chatting",
    backToGallery: "Back to Gallery",
  },

  // Breadcrumb
  breadcrumb: {
    workspace: "Workspace",
    chats: "Chats",
  },

  // Workspace
  workspace: {
    officialWebsite: "ActWeave's official website",
    githubTooltip: "ActWeave on GitHub",
    settingsAndMore: "Settings and more",
    visitGithub: "ActWeave on GitHub",
    reportIssue: "Report an issue",
    contactUs: "Contact us",
    about: "About ActWeave",
    logout: "Log out",
    gatewayUnavailable: "Gateway is temporarily unavailable.",
    gatewayUnavailableRetrying: "Retrying in the background…",
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
    agentModelUnavailableTitle: "Agent model unavailable",
    agentModelUnavailableDescription:
      "The Agent's configured model could not be resolved. Check its active binding, published version, and active model catalog entry, then retry.",
    runExecutionProfile: (modelName, modeName, supportsVision) =>
      `Effective run: ${modelName} · ${modeName} · ${supportsVision ? "vision-capable" : "text only"}`,
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
      "Are you sure you want to delete this side chat? This action cannot be undone. To simply hide it, use the side chat toggle in the header instead.",
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

  // Subtasks
  uploads: {
    uploading: "Uploading...",
    uploadingFiles: "Uploading files, please wait...",
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
    note: "Header totals use persisted thread usage, plus visible in-flight usage while a run is still streaming. Per-turn and debug usage come from currently visible messages only. Totals may differ from provider billing pages.",
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
      notification: "Notification",
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
        "Delete long-term memory and pending content for this account in every project.",
      resetButton: "Reset",
      resetDialogTitle: "Reset all Memory?",
      resetDialogDescription:
        "This permanently deletes your long-term document, pending history, versions, Dream records, and Memory snapshots. It cannot be undone.",
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
    notification: {
      title: "Notification",
      description:
        "ActWeave only sends a completion notification when the window is not active. This is especially useful for long-running tasks so you can switch to other work and get notified when done.",
      requestPermission: "Request notification permission",
      deniedHint:
        "Notification permission was denied. You can enable it in your browser's site settings to receive completion alerts.",
      testButton: "Send test notification",
      testTitle: "ActWeave",
      testBody: "This is a test notification.",
      notSupported: "Your browser does not support notifications.",
      disableNotification: "Disable notification",
    },
    account: {
      profileTitle: "Profile",
      email: "Email",
      role: "Role",
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
    email: "Email",
    emailPlaceholder: "you@example.com",
    password: "Password",
    passwordPlaceholder: "•••••••",
    rememberMe: "Remember this session and email",
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
