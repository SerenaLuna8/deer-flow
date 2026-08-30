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
    skipToContent: "Skip to main content",
    home: "Home",
    settings: "Settings",
    delete: "Delete",
    edit: "Edit",
    rename: "Rename",
    share: "Share",
    openInNewWindow: "Open in new window",
    close: "Close",
    closeArtifacts: "Close artifacts",
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
    reasoningSummaryInProgress: (seconds?: number) =>
      seconds === undefined
        ? "Summarizing reasoning…"
        : `Summarizing reasoning… (${seconds}s)`,
    reasoningSummaryFor: (seconds?: number) =>
      seconds === undefined
        ? "Reasoning summary"
        : seconds === 0
          ? "Reasoning summary (under 1 second)"
          : `Reasoning summary (${seconds} ${seconds === 1 ? "second" : "seconds"})`,
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

  runExecutionState: {
    unavailable: "Execution status temporarily unavailable",
    totalDuration: (duration) => `Total execution time ${duration}`,
    phaseDuration: (duration) => `Current phase ${duration}`,
    phases: {
      queued: "Waiting for an execution slot",
      waiting_for_worker: "Waiting for an execution Worker",
      starting: "Worker claimed; starting",
      executing: "Executing",
      retry_wait: "Waiting to retry",
      waiting_for_lease_expiry: "Worker disconnected; waiting for lease expiry",
      waiting_for_terminalization:
        "Execution outcome unknown; waiting for safe settlement",
      waiting_for_recovery: "Waiting to recover execution",
      recovering: "Recovering execution",
      cancelling: "Stopping",
    },
  },

  // Home
  home: {
    docs: "Docs",
  },

  // Welcome
  welcome: {
    greeting: "Hello, again!",

    createYourOwnSkill: "Create Your Own Skill",
    createYourOwnSkillDescription:
      "Create your own skill to release the power of Fluva. With customized skills,\nFluva can help you search on the web, analyze data, and generate\n artifacts like slides, web pages and do almost anything.",
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

  // Knowledge bases
  knowledge: {
    page: {
      eyebrow: (projectName) => `${projectName} · Knowledge`,
      title: "Knowledge",
      description:
        "Manage knowledge bases and documents, and test retrieval quality.",
    },
    tabs: {
      bases: "Bases",
      search: "Retrieval test",
    },
    common: {
      back: "Back",
      cancel: "Cancel",
      save: "Save",
      saving: "Saving…",
      create: "Create",
      creating: "Creating…",
      delete: "Delete",
      deleting: "Deleting…",
      edit: "Edit",
    },
    status: {
      active: "Active",
      disabled: "Disabled",
      deleting: "Deleting",
    },
    documentStatus: {
      uploading: "Uploading",
      queued: "Queued",
      processing: "Processing",
      ready: "Ready",
      failed: "Failed",
      deleting: "Deleting",
    },
    bases: {
      title: "Knowledge bases",
      empty: "No knowledge bases yet. Create one to start uploading documents.",
      createButton: "New base",
      createTitle: "New knowledge base",
      createDescription:
        "Pick a model configuration for this base; it cannot be changed later.",
      nameLabel: "Name",
      namePlaceholder: "e.g. Product handbook",
      descriptionLabel: "Description (optional)",
      descriptionPlaceholder: "What this base is for",
      modelLabel: "Embedding model",
      modelPlaceholder: "Select an embedding model",
      modelHint: "Changing it later requires rebuilding every document.",
      noModels:
        "No embedding models available. Ask a system administrator to add one in model management.",
      modelsLoadFailed: "Models could not be loaded. Try again later.",
      editTitle: "Edit knowledge base",
      statusLabel: "Status",
      deleteTitle: "Delete knowledge base",
      deleteDescription: (name) =>
        `This deletes "${name}" with all documents, segments, and stored files. This cannot be undone.`,
      deleteConfirm: "Delete",
      deleteError: (message) => `Delete failed: ${message}`,
      documentCount: (count) =>
        `${count} ${count === 1 ? "document" : "documents"}`,
      updatedAt: (time) => `Updated ${time}`,
      openDocuments: "View documents",
      noDescription: "No description yet.",
      retrievalSectionTitle: "Retrieval defaults",
      defaultTopKLabel: "Default results (top_k)",
      defaultTopKHint: "Used when a search omits top_k; range 1-20.",
      defaultThresholdLabel: "Default score threshold",
      defaultThresholdHint:
        "Used when a search omits the threshold; 0 disables filtering.",
      rerankerLabel: "Reranker model",
      rerankerNone: "No reranking",
      rerankerHint: "Optional. Takes effect on save; no rebuild required.",
      rebuildSectionTitle: "Embedding model",
      rebuildModelLabel: "Embedding model",
      rebuildHint:
        "Rebuilding rebinds the embedding model and re-embeds every document one by one; documents are excluded from retrieval until they finish.",
      rebuildButton: "Rebuild embeddings",
      rebuildPending: "Rebuilding…",
      rebuildStarted: "Rebuild started; documents will reprocess one by one.",
      rebuildConfirmTitle: "Rebuild knowledge base embeddings",
      rebuildConfirmDescription: (name) =>
        `This rebinds "${name}" to the selected embedding model and re-embeds every document. Documents are excluded from retrieval while rebuilding.`,
      rebuildConfirm: "Rebuild",
    },
    wizard: {
      heroTitle: "Create your first knowledge base",
      uploadCreateTitle: "Create from documents",
      uploadCreateHint:
        "Upload documents; chunking and indexing run automatically. The fastest way to start.",
      orSeparator: "or",
      emptyCreateTitle: "Create an empty base",
      emptyCreateHint: "Create the base first and upload documents later.",
      steps: {
        source: "Choose data source",
        configure: "Chunking & model",
        finish: "Process & finish",
      },
      stepBadge: (step) => `STEP ${step}`,
      sourceSectionTitle: "Upload text files",
      dropzoneTitle: "Drag files here, or click to browse",
      removeFile: (name) => `Remove ${name}`,
      filesSelected: (count) =>
        `${count} ${count === 1 ? "file" : "files"} selected`,
      next: "Next",
      previous: "Previous",
      saveAndProcess: "Save & process",
      chunkSectionTitle: "Chunk settings",
      infoSectionTitle: "Base details",
      createdTitle: "Knowledge base created",
      createdHint:
        "Documents are being embedded. Once ready they are searchable in retrieval tests and agent chats.",
      processingTitle: "Embedding progress",
      summaryTitle: "Settings",
      goToDocuments: "Go to documents",
      uploadFailedNote:
        "These files failed to upload; you can upload them again from the documents page:",
      previewTitle: "Chunk preview",
      previewHint: (fileName) => `Previewing the first file: ${fileName}`,
      previewLoading: "Generating preview…",
      previewRefresh: "Refresh preview",
      previewStale:
        "Preview is out of date. Refresh to apply the current settings.",
      previewInvalid:
        "Fix the invalid chunk settings before refreshing the preview.",
      previewTotal: (total) => `${total} chunks in total`,
      previewChunkLabel: (position) =>
        `Chunk-${String(position).padStart(2, "0")}`,
      previewCharacters: (count) => `${count} characters`,
      previewChildCount: (count) =>
        `${count} child ${count === 1 ? "chunk" : "chunks"}`,
      previewChildLabel: (index) => `C-${String(index).padStart(2, "0")}`,
    },
    detail: {
      navLabel: "Knowledge base sections",
      documents: "Documents",
      settings: "Settings",
      settingsSaved: "Saved.",
      metadata: "Metadata",
    },
    metadata: {
      title: "Metadata fields",
      description:
        "Define custom fields assignable to documents; searches can filter on them.",
      empty: "No metadata fields yet.",
      addButton: "Add field",
      addTitle: "Add metadata field",
      nameLabel: "Field name",
      namePlaceholder: "e.g. department, published_year",
      typeLabel: "Field type",
      typeString: "Text",
      typeNumber: "Number",
      typeTime: "Time",
      columns: {
        name: "Name",
        type: "Type",
        actions: "Actions",
      },
      rename: "Rename",
      renameTitle: (name) => `Rename field "${name}"`,
      delete: "Delete",
      deleteTitle: "Delete metadata field",
      deleteDescription: (name) =>
        `This deletes the field "${name}" and clears its value from every document. This cannot be undone.`,
    },
    documents: {
      title: (baseName) => `${baseName} · Documents`,
      empty: "No documents yet. Upload one to get started.",
      uploadButton: "Upload document",
      uploadTitle: "Upload document",
      uploadDescription:
        "PDF, DOCX, TXT, Markdown, CSV, XLSX, HTML, PPTX, and EPUB up to 50 MB per file.",
      fileLabel: "File",
      displayNameLabel: "Display name (optional)",
      displayNamePlaceholder: "Defaults to the file name",
      chunkSizeLabel: "Chunk size (characters)",
      chunkSizeHint: "Allowed range: 200-4000 characters.",
      chunkOverlapLabel: "Chunk overlap (characters)",
      chunkOverlapHint:
        "Allowed range: 0-500 characters, smaller than the chunk size.",
      chunkSeparatorLabel: "Delimiter",
      chunkSeparatorHint:
        "Preferred split boundary; \\n stands for a line break (default \\n\\n).",
      chunkingModeLabel: "Chunking mode",
      chunkingModeGeneral: "General",
      chunkingModeGeneralHint:
        "Each chunk is embedded on its own and recalled directly.",
      chunkingModeParentChild: "Parent-child",
      chunkingModeParentChildHint:
        "Parents carry full context; children are embedded for recall and roll hits up to their parent.",
      childChunkSizeLabel: "Child chunk size (characters)",
      childChunkSeparatorLabel: "Child delimiter",
      childChunkSeparatorHint:
        "Children are split inside each parent at this delimiter (default \\n).",
      preprocessingLabel: "Text pre-processing rules",
      removeExtraSpacesLabel: "Replace consecutive spaces, newlines and tabs",
      removeUrlsEmailsLabel: "Delete all URLs and email addresses",
      chunkImmutableNote:
        "Chunk settings cannot be changed after upload; retry reuses the original settings.",
      upload: "Upload",
      uploading: "Uploading…",
      uploadingProgress: (done, total) => `Uploading… (${done}/${total})`,
      uploadResultSuccess: (name) => `${name}: uploaded`,
      uploadResultFailed: (name, message) => `${name}: ${message}`,
      columns: {
        name: "Name",
        status: "Status",
        enabled: "Enabled",
        size: "Size",
        segments: "Segments",
        words: "Characters",
        actions: "Actions",
      },
      retry: "Retry",
      download: "Download original",
      delete: "Delete",
      deleteTitle: "Delete document",
      deleteDescription: (name) =>
        `This deletes "${name}" with all segments and the stored file. This cannot be undone.`,
      viewSegments: "View segments",
      rename: "Rename",
      renameTitle: "Rename document",
      renameLabel: "Display name",
      actionsAria: (name) => `Actions for ${name}`,
      enableAria: (name) => `Enable ${name}`,
      disableAria: (name) => `Disable ${name}`,
      selectAllAria: "Select all documents",
      selectRowAria: (name) => `Select ${name}`,
      selectedCount: (count) =>
        `${count} ${count === 1 ? "document" : "documents"} selected`,
      batchEnable: "Enable",
      batchDisable: "Disable",
      batchDelete: "Delete",
      clearSelection: "Clear selection",
      batchDeleteTitle: "Delete selected documents",
      batchDeleteDescription: (count) =>
        `This deletes ${count} ${count === 1 ? "document" : "documents"} with all segments and stored files. This cannot be undone.`,
      metadataAction: "Metadata",
      metadataTitle: (name) => `Edit metadata · ${name}`,
      metadataEmpty:
        "This knowledge base has no metadata fields yet. Define them under Metadata first.",
      metadataClearHint: "Leave a value empty to clear that field.",
    },
    segments: {
      title: (documentName) => `${documentName} · Segments`,
      empty: "This document has no segments.",
      position: (position) => `Segment #${position}`,
      pageInfo: (page, pageCount, total) =>
        `Page ${page} of ${pageCount} · ${total} total`,
      previousPage: "Previous",
      nextPage: "Next",
      close: "Close",
      stats: (segments, words) =>
        `${segments} ${segments === 1 ? "segment" : "segments"} · ${words.toLocaleString()} characters`,
      add: "Add segment",
      addTitle: "Add segment",
      addDescription:
        "The segment is embedded with this base's embedding model and becomes retrievable immediately.",
      contentLabel: "Content",
      contentPlaceholder: "Enter the segment content",
      edit: "Edit",
      editTitle: (position) => `Edit segment #${position}`,
      delete: "Delete",
      deleteTitle: "Delete segment",
      deleteDescription: (position) =>
        `This deletes segment #${position} and its vector. This cannot be undone.`,
      enableAria: (position) => `Enable segment #${position}`,
      disableAria: (position) => `Disable segment #${position}`,
      wordCount: (count) => `${count.toLocaleString()} characters`,
      manualBadge: "Manual",
    },
    sourcePosition: {
      page: (page) => `Page ${page}`,
      paragraph: (paragraph) => `Paragraph ${paragraph}`,
      row: (row) => `Row ${row}`,
      slide: (slide) => `Slide ${slide}`,
      chapter: (chapter) => `Chapter ${chapter}`,
    },
    search: {
      title: "Retrieval test",
      description:
        "Run retrieval against this knowledge base to verify recall. With a reranker bound, results are ordered by rerank scores (0 to 1); otherwise by cosine similarity (-1 to 1).",
      queryLabel: "Query",
      queryPlaceholder: "Enter a question or keywords",
      baseFilterLabel: "Limit to bases (optional)",
      allBases: "All bases",
      topKLabel: "Results (top_k)",
      topKHint: "Leave empty to use the base default.",
      thresholdLabel: "Score threshold",
      thresholdHint: "Leave empty to use the base default; 0 disables filtering.",
      submit: "Search",
      searching: "Searching…",
      empty: "No matching content found",
      resultsTitle: (count) => `${count} ${count === 1 ? "match" : "matches"}`,
      score: (score) => `Retrieval score ${score.toFixed(3)}`,
      recentTitle: "Recent queries",
      recentEmpty: "No queries recorded yet.",
      recentColumns: {
        query: "Query",
        source: "Source",
        results: "Matches",
        topScore: "Top retrieval score",
        time: "Time",
      },
      recentSource: {
        agent: "Agent call",
        retrieval_test: "Retrieval test",
      },
      filtersLabel: "Metadata filters",
      filtersHint:
        "Conditions combine with AND; only documents matching all of them are recalled.",
      addFilter: "Add condition",
      removeFilterAria: (index) => `Remove condition ${index}`,
      filterFieldAria: (index) => `Condition ${index} field`,
      filterOperatorAria: (index) => `Condition ${index} operator`,
      filterValueAria: (index) => `Condition ${index} value`,
      filterValuePlaceholder: "Filter value",
      filterNoFields:
        "This knowledge base has no metadata fields, so no filters can be added.",
      operators: {
        eq: "equals",
        contains: "contains",
        gte: "≥",
        lte: "≤",
      },
    },
    citations: {
      summary: (count) =>
        `Cited ${count} knowledge ${count === 1 ? "source" : "sources"}`,
      score: (score) => `Retrieval score ${score.toFixed(3)}`,
      segmentPosition: (position) => `Segment #${position}`,
    },
    errors: {
      generic: "The operation failed. Please try again.",
      network: "Network error. Check your connection and retry.",
      invalidResponse: "The server returned an unexpected response.",
    },
  },

  // Workspace Changes
  workspaceChanges: {
    title: "Workspace changes",
    editedTitle: (count) => `Edited ${count} ${count === 1 ? "file" : "files"}`,
    badge: (count, additions, deletions) =>
      additions === null || deletions === null
        ? `${count} ${count === 1 ? "file" : "files"} changed`
        : `${count} ${count === 1 ? "file" : "files"} changed +${additions} -${deletions}`,
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
      "Dictate with voice. Fluva receives only transcribed text; audio is handled by your browser or system speech service.",
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
    researchWorkload: "Research",
    researchWorkloadDescription:
      "Use for the next send only; resets to Interactive after Run admission.",
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
    compactSourceTooLarge:
      "A complete chat turn cannot be safely compacted within the bounded workload. Shorten that turn or ask a platform administrator to increase the summary budget.",
    compactPromptBudgetTooSmall:
      "The summary token budget is too small for the compaction prompt. Ask a platform administrator to increase it.",
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
    dreamModelUnavailable:
      "The configured Dream model is unavailable. Ask a platform administrator to select an active model, then try again.",
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
    navigation: {
      label: "Project navigation",
      menuLabel: "Project menu",
      workspaceAria: "Fluva workspace",
      overview: "Overview",
      conversations: "Chats",
      automations: "Automations",
      agents: "Agent",
      knowledge: "Knowledge",
      skills: "Skill",
      mcp: "MCP",
      memory: "Memory",
      connections: "Connections",
      audit: "Audit log",
      settings: "Project settings",
      backToWorkspace: "Back to workspace",
      sections: {
        work: "Work",
        capabilities: "Capabilities",
        management: "Project management",
      },
      account: "Account",
      platformAdministration: "Platform administration",
      systemSettings: "System settings",
      logout: "Log out",
      expand: "Expand project menu",
      collapse: "Collapse project menu",
      open: "Open project navigation",
      sheetTitle: "Project navigation",
      sheetDescription: "Navigate this project",
    },
    accessDenied: {
      title: "You do not have access",
      description: (area) =>
        `You are a member of this project, but your current role cannot access ${area}. Ask a project administrator to change your role if you need access.`,
      backToProject: "Back to project overview",
    },
    settings: {
      accessArea: "project settings",
      governanceEyebrow: (projectName) => `${projectName} · Governance`,
      title: "Project settings",
      description: "Manage project details, members, and lifecycle.",
      navigationLabel: "Project settings",
      navigation: {
        general: {
          label: "General settings",
          description: "Project details and lifecycle",
        },
        members: {
          label: "Project members",
          description: "Member roles and invitations",
        },
      },
      general: {
        title: "Project details",
        description:
          "This information appears in the workspace and project navigation.",
        displayName: "Project name",
        icon: "Icon",
        slug: "Project slug",
        slugDescription:
          "The project slug is used in its URL and cannot be changed after creation.",
        projectDescription: "Project description",
        descriptionPlaceholder: "Describe this project's purpose and goals",
        saved: "Project details saved",
        saving: "Saving…",
        save: "Save changes",
      },
      lifecycle: {
        title: "Project lifecycle",
        description:
          "Requesting deletion immediately blocks entry and governance, then starts a 30-day recovery window. Self-service recovery is unavailable after that window.",
        requestDeletion: "Request project deletion",
        confirmTitle: "Delete this project?",
        confirmDescription:
          "The project will immediately enter pending deletion and you will return to the workspace. A project administrator can undo this action during the 30-day recovery window.",
        cancel: "Cancel",
        confirm: "Confirm deletion request",
      },
    },
    members: {
      managementEyebrow: (projectName) => `${projectName} · Project management`,
      title: "Members and invitations",
      description: "Manage project members, roles, and pending invitations.",
      inviteMember: "Invite member",
      membersTitle: "Members",
      invitationsTitle: "Invitations",
      emptyInvitations: "No project invitations.",
      columns: {
        account: "Account",
        role: "Role",
        status: "Status",
        actions: "Actions",
      },
      roles: {
        admin: "Admin",
        editor: "Editor",
        runner: "Runner",
        viewer: "Viewer",
      },
      membershipStatuses: {
        active: "Active",
        left: "Left",
        removed: "Removed",
      },
      invitationStatuses: {
        pending: "Pending",
        redeemed: "Accepted",
        revoked: "Revoked",
        expired: "Expired",
      },
      actions: {
        changeRole: "Change role",
        removeMember: "Remove member",
        leaveProject: "Leave project",
        revokeInvitation: "Revoke invitation",
      },
      confirmations: {
        removeTitle: "Remove this member?",
        removeDescription: (email) =>
          `Removing ${email} immediately revokes their access to this project.`,
        removeConfirm: "Remove member",
        leaveTitle: "Leave this project?",
        leaveDescription:
          "You will lose access to this project unless you are invited again.",
        leaveConfirm: "Leave project",
        revokeTitle: "Revoke this invitation?",
        revokeDescription: (email) =>
          `The invitation link sent to ${email} will stop working immediately.`,
        revokeConfirm: "Revoke invitation",
        cancel: "Cancel",
      },
      inviteDialog: {
        title: "Invite member",
        description:
          "The invitation link is shown only once. Send it through a trusted channel.",
        inviteLink: "Invitation link",
        done: "Done",
        email: "Email",
        projectRole: "Project role",
        create: "Create invitation",
      },
      roleDialog: {
        title: "Change member role",
        projectRole: "Project role",
        save: "Save role",
      },
    },
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
      "As you keep talking, Fluva records pending notes and Dream organizes them into this document.",
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
    dreamModelUnavailable:
      "The configured Dream model is unavailable. Ask a platform administrator to select an active model, then try again.",
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
      settings: "Model management",
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
        privateRunWorkerCount: "Private-run Worker processes",
        privateRunWorkerCapacity: "Private-run Worker capacity",
        oldestHeartbeat: "Oldest Worker heartbeat",
        schedulerOwnership: "Scheduler ownership",
        runSkillWriterMode: "Run Skill writer mode",
        runSkillWriterArtifact: "Run Skill writer artifact",
        legacyPolicyDigest: "Legacy policy digest",
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
          v4_reference: "Version reference v4",
          legacy_v3: "Legacy v3 rollback",
          unknown: "Unknown",
        },
        components: {
          database: "Database",
          schema: "Schema",
          worker_fleet: "Worker fleet",
          private_run_worker_fleet: "Private-run Worker fleet",
          run_skill_writer: "Run Skill writer",
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
        readyJobs: "Ready jobs",
        oldestReadyJobAge: "Oldest ready job age (seconds)",
        staleLeases: "Stale leases",
        waitingForWorkerRuns: "Runs waiting for a Worker",
        waitingForTerminalizationRuns: "Runs waiting for terminalization",
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
      shortProjectId: (projectId) => `UUID ${projectId}`,
      projectIdentityUnavailable: "Project identity could not be loaded.",
    },
    common: {
      assetVersion: "Asset revision",
      versionHistory: "Version history",
      retry: "Retry",
      retrying: "Retrying…",
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
        "This configuration is not active yet. Configure its Project-owned secrets; once active, a Worker will automatically test the service and read its tool inventory.",
      neverDiscovered:
        "No tool discovery has completed yet. You can test the service and read its tool inventory now.",
      testing: "Testing the service and reading tools…",
      catalogInvalid:
        "The latest discovery returned an unsafe tool inventory. Check the MCP service tool names, descriptions, and parameter schemas, then test again.",
      discoveryUnavailable:
        "The latest connection to the MCP service failed. Check service availability, outbound proxy, and network configuration, then test again.",
      stale:
        "The MCP configuration or its secrets changed, so the previous tool inventory is stale. Test again.",
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
    catalog: {
      assetCatalog: "Asset catalog",
      projectAgentTitle: "Project Agents",
      projectSkillTitle: "Project Skills",
      projectMcpTitle: "Project MCP",
      systemAssets: "System assets",
      systemAssetsDescription:
        "System assets are shared read-only. Project bindings pin an explicit version and never upgrade automatically.",
      systemCurrentAssetsDescription:
        "System Agents and Skills are shared read-only. Projects bind the asset; runtime resolves the Agent Definition and Skill Current Version.",
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
      definitionStatus: "Definition status",
      definitionAvailable: "Definition available",
      definitionMissing: "Definition unavailable",
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
      catalogUnavailable: "The asset catalog could not be loaded.",
      versionHistoryUnavailable: "Version history could not be loaded.",
      projectCatalogUnavailable: "Project assets could not be loaded.",
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
        "Owned by this project. Saving updates its single Agent Definition for future Runs.",
      projectVersionedDescription:
        "Owned by this project. Content changes create immutable new versions.",
      noProjectAssets: "This project has no assets of this type.",
      project: "Project",
      publishStatus: "Publication",
      createNewVersion: "Create new version",
      waitingForAdmin: "Waiting for administrator approval",
      archive: "Archive",
      activate: "Enable",
      disable: "Disable",
      suspend: "Suspend",
    },
    version: {
      current: "Current Version",
      candidate: "Candidate Version",
      historical: "Historical Version",
      currentUnconfirmed: "The current configuration could not be confirmed.",
      activate: "Activate version",
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
    },
    diff: {
      payloadChecksum: "Payload checksum",
      description: "Description",
      model: "Model",
      toolGroups: "Tool groups",
      skillAssets: "Skill assets",
      mcpVersions: "MCP configurations",
      compatibility: "Compatibility",
      files: "Files",
      secretRequirements: "Secret requirements",
      transport: "Transport",
      command: "Command",
      url: "URL",
      arguments: "Arguments",
      timeout: "Timeout",
      secretSlots: "Secret slots",
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
      projectCredentialTargetsOnly:
        "Project MCP credential parameters can be sent only as headers or query parameters. The historical configuration can be viewed but cannot be published, bound, or used by an Agent.",
      missingSystemCommand:
        "This stdio system MCP has no command and cannot be bound or used by an Agent.",
      missingSystemUrl:
        "This remote system MCP has no URL and cannot be bound or used by an Agent.",
      systemEnvOnly:
        "Stdio system MCP secret slots support env only and cannot otherwise be bound or used by an Agent.",
      systemRemoteSecretsOnly:
        "Remote system MCP secret slots support headers, query parameters, or oauth only and cannot otherwise be bound or used by an Agent.",
    },
    dialogs: {
      authoring: {
        title: (name) => `Create a version for ${name}`,
        skillDescription:
          "System Skill v1 cannot be upgraded. A Project Skill can save a new Candidate Version.",
        mcpDescription:
          "An MCP definition declares secret slots but never stores secret values in the version definition.",
        description: "Description",
        secretSlots: "Secret slots JSON",
        invalidSecretSlots: "Secret slots must be a valid JSON array.",
        cancel: "Cancel",
        saving: "Saving…",
        save: "Save version",
      },
      binding: {
        switchTitle: "Switch project binding version",
        enableTitle: "Enable system asset",
        description: (name) =>
          `${name}. This changes only the current project binding. It never modifies the packaged system definition or version.`,
        agentDescription: (name) =>
          `${name}. This project only enables or disables the System Agent's single read-only Definition.`,
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
        currentVersion: "Current Version",
        currentVersionUnavailable: "The Current Version is unavailable.",
        currentVersionInUse: "Current Version in use",
      },
    },
    errors: {
      notFound: "The asset does not exist or is no longer visible.",
      forbidden: "This account cannot perform that action.",
      conflict: "The asset changed. Refresh and try again.",
      validationFailed:
        "The submitted content does not meet asset requirements.",
      mcpVersionValidation:
        "The MCP configuration failed validation. Confirm the transport is HTTP (Streamable HTTP) or SSE; the URL has no embedded secrets, query parameters, or fragments; the host is exactly localhost or a canonical IPv4/IPv6 literal rather than an ordinary DNS hostname; localhost is case-insensitive and is treated as 127.0.0.1, while IPv6 loopback is entered explicitly as [::1]; the IP belongs to an administrator-configured allowed network range; and every credential group contains at least one header or query field (or both) with valid field names. Network ranges are configured by platform administrators, not in this form. If an administrator just changed allowed ranges, restart Gateway, Scheduler, and Worker.",
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
        "Create a task and let Fluva automatically complete work on schedule.",
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
        title: "Agent blueprint",
        triggerLabel: "Blueprint",
        openAria: "Open Agent blueprint",
        conflictCount: (count) =>
          `${count} unresolved ${count === 1 ? "conflict" : "conflicts"}`,
        readyTitle: "Agent blueprint ready",
        summary: (conflictCount) =>
          conflictCount > 0
            ? `4 documents · ${conflictCount} unresolved ${conflictCount === 1 ? "conflict" : "conflicts"}`
            : "4 documents",
        viewBlueprint: "View blueprint",
        closeAria: "Close Agent blueprint",
        panelSummary: (conflictCount) =>
          conflictCount > 0
            ? `4 documents · ${conflictCount} unresolved ${conflictCount === 1 ? "conflict" : "conflicts"}`
            : "4 documents",
        tabsAria: "Agent blueprint content",
        overviewTab: "Overview",
        documentsTab: "Documents",
        runtime: "Runtime configuration",
        noDescription: "No Agent description has been generated.",
        nameLabel: "Agent name",
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
        openConflictDocument: (name) => `Open ${name} in documents`,
        blockingConflictHint:
          "Continue the conversation so the Agent regenerates the blueprint. The Agent cannot be created until all red conflicts disappear.",
        createHint:
          "Creates the initial Agent Definition. The Agent starts suspended and must be enabled manually.",
        creating: "Creating…",
        createAgent: "Create Agent",
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
        completedRecord: "Initial Agent Definition · Read-only",
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
        stopGeneration: "Stop this generation",
        stoppingGeneration: "Stopping…",
        viewAgent: "View Agent",
        activity: {
          title: "Thinking and execution",
          reasoning: (value) =>
            value === 1 ? "Model reasoning" : "Repair reasoning",
          duration: (value) => `${(value / 1000).toFixed(1)}s`,
          terminal: {
            completed: "Completed",
            failed: "Failed",
            stopped: "Stopped",
            cancelled: "Cancelled",
          },
        },
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
      editDescription:
        "Edit the four fixed Markdown documents in the current Agent Definition. Saving applies to future Runs immediately.",
      readOnlyDescription:
        "Read-only instruction documents for the System Agent's sole immutable Definition.",
      adminProjectReadOnlyDescription:
        "Read-only view of this Project Agent's current mutable Definition. Project saves replace it for future Runs.",
      edit: "Edit",
      fixedFiles: "Fixed files",
      displayMode: "Display mode",
      source: "Source",
      preview: "Preview",
      empty: "No content",
      editFile: (name) => `Edit ${name}`,
      saveHint:
        "Saving immediately updates the Agent Definition used by future Runs.",
      discard: "Discard changes",
      saving: "Saving…",
      save: "Save",
      permissionLost:
        "Editing permission was revoked. Local changes are preserved but cannot be saved yet.",
      recoveryPreserved:
        "The server Definition changed. Local changes were preserved; review them before saving again.",
      recoverySynced:
        "The server Definition changed and the latest Definition has been loaded.",
      recoveryFailed:
        "The latest Definition could not be loaded. Local changes were preserved.",
      recoveryReloading: "Loading the latest Definition…",
      invalidResponse: "The server returned an invalid Agent Definition.",
      conflictDetected: "A Definition conflict was detected.",
      reloadRequired: "Reload the latest Definition before editing.",
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
        noCurrentVersion: "No Current Version",
      },
      remediation: {
        restoreSystemAsset: "Ask an administrator to restore the system asset",
        enableSystemBinding:
          "Ask an administrator to enable the system binding",
        activateCandidateVersion:
          "Activate a Candidate Version of this project asset first",
        activateProjectAsset: "Activate this project asset first",
      },
      explanationSeparator: "; ",
      boundCount: (value) => `${value} bound`,
      unavailablePrefix: (reason) => `Unavailable: ${reason}`,
      remediationPrefix: (reason) => `Next step: ${reason}`,
      historicalDisabled:
        "Unavailable historical bindings can only be removed.",
      historicalVersion: "Unavailable historical binding",
      historicalVersionDescription:
        "The target is no longer present in the current capability catalog.",
      permissionLost:
        "Editing permission was revoked. Local changes are preserved but cannot be saved yet.",
      recoverySynced:
        "The server Definition changed and the latest Definition has been loaded.",
      recoveryPreserved:
        "The server Definition changed. Local changes were preserved; review them before saving again.",
      recoveryFailed:
        "The latest Definition could not be loaded. Local changes were preserved.",
      recoveryReloading: "Loading the latest Definition…",
      conflictDetected: "A Definition conflict was detected.",
      reloadRequired: "Reload the latest Definition before editing.",
      reloading: "Reloading…",
      permissionBlocked: "Your account cannot edit Agent capabilities.",
      preparingDefinition: "Preparing the Agent Definition…",
      catalogLoading: "Loading capability catalog…",
      catalogLoadFailed: "Capability catalog could not be loaded.",
      validatingMcp: "Validating MCP dependencies…",
      mcpValidationFailed: "MCP dependency validation failed.",
      title: "Capability bindings",
      description:
        "Choose the tool groups, Skills, and MCPs this Agent can use.",
      saving: "Saving…",
      save: "Save",
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
      viewDesignRecord: "View design record",
      instructionsTab: "Instructions",
      capabilitiesTab: "Capabilities",
      viewModeAria: "Agent catalog view",
      cards: "Cards",
      list: "List",
      chatForbidden: "Your account cannot create chats in this project.",
      unavailable: "This Agent is unavailable.",
      executeForbidden: "Your account cannot run Agents.",
      definitionRequired: "The Agent Definition is unavailable.",
      defaultAdminOnly: "Only administrators can set the default Agent.",
      defaultUnavailable: "This Agent cannot be set as default.",
      systemDefaultUnavailable:
        "Enable this system Agent in the project first.",
      mainUnavailable: "The main Agent is unavailable.",
      mainExecuteForbidden: "Your account cannot run the main Agent.",
      mainDefinitionUnavailable: "The main Agent has no available Definition.",
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
    catalog: {
      viewDesignRecord: "View design record",
    },
    export: {
      label: "Export ZIP",
      preparing: "Preparing…",
      started: "Download started",
    },
    secrets: {
      workbenchAria: "Skill editing area",
      filesTab: "Files",
      secretsTab: "Runtime secrets",
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
      nameLabel: "Secret name",
      namePlaceholder: "provider_key",
      targetEnvLabel: "Sandbox environment variable",
      targetEnvPlaceholder: "API_KEY",
      newOptional: "Make the new variable optional",
      add: "Add",
      invalidName:
        "The name must start with a letter or underscore and contain only letters, numbers, and underscores.",
      invalidTargetEnv:
        "The Sandbox environment variable must use letters, numbers, and underscores and cannot start with a number.",
      duplicateName:
        "That secret name is already declared. Names are case-sensitive.",
      duplicateTargetEnv:
        "That Sandbox environment variable is already targeted by this Skill.",
      autonomousTitle: "Secret injection mode",
      autonomousDescription:
        "Choose which Skill activation modes may inject already configured secrets.",
      autonomousAria: "Choose the secret injection mode",
      advancedSettings: "Advanced settings",
      injectionAutomatic: "Inject after the Skill is read",
      injectionAutomaticDescription:
        "The Agent may inject configured secrets after reading this Skill. Explicit /Skill activation also injects them.",
      injectionExplicit: "Inject only after explicit /Skill activation",
      injectionExplicitDescription:
        "Natural-language automatic loading does not inject secrets. Injection requires explicit /Skill activation.",
      location: (line, column) =>
        `Line ${line}${column === null ? "" : `, column ${column}`}: `,
      loadSourceFailed:
        "The root SKILL.md could not be loaded, so environment variable editing is unavailable.",
      loadSource: "Loading SKILL.md…",
      saveBlocked:
        "Wait for the SKILL.md check to finish or fix the environment variable declaration",
    },
    activationDialog: {
      title: "Activate Skill Candidate Version",
      description: (version) =>
        `Activate version ${version}. This dialog checks the exact version's required Skill secrets.`,
      loading: "Checking runtime requirements…",
      noRequirements:
        "The target version declares no environment variables and can be activated directly.",
      bindingsTitle: "Version secrets",
      required: "Required",
      optional: "Optional",
      statusConfigured: "Configured",
      statusMissing: "Not configured",
      statusInvalid: "Needs repair",
      preflightReady: "Runtime requirement checks passed",
      preflightBlocked: "Runtime requirement checks did not pass",
      preflightSummary: (configuredRequired, required, invalid) =>
        `${configuredRequired}/${required} required mappings configured; ${invalid} invalid mapping${invalid === 1 ? "" : "s"}.`,
      configureSecrets: "Configure version secrets",
      approvalRequiredForActive:
        "Every Candidate Version must have all required secrets configured before activation. Ask a Project Admin to configure them first.",
      cancel: "Cancel",
      retry: "Check again",
      activating: "Activating…",
      activate: "Activate version",
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
        permissionReadOnlyCreate:
          "Your account cannot continue creating this Skill. Saved session content and candidate files remain available to view.",
        permissionReadOnlyRevise:
          "Your account cannot continue revising this Skill. Saved session content and candidate files remain available to view.",
        creatingSkill: "Creating Skill…",
        creatingCandidate: "Creating a Candidate Version…",
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
        completedRecord: (version) =>
          `${version === null ? "Candidate Version" : `Candidate Version v${version}`} · Read-only`,
        unsavedChanges: "Unsaved changes",
        agentRunning: "The Builder Agent is running",
        checkedCreate: "Checked; ready to create",
        checkedRevise: "Checked; ready to save a Candidate Version",
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
        secretsSurface: "Runtime secrets",
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
        requiredSecrets: "Required secrets:",
        checkSkill: "Check Skill",
        commitCreate: "Create Skill (disabled by default)",
        commitRevise: "Save Candidate Version",
      },
      activity: {
        title: "Thinking and execution",
        terminal: {
          completed: "Completed",
          failed: "Failed",
          stopped: "Stopped",
        },
        duration: (milliseconds) => `· ${(milliseconds / 1000).toFixed(1)}s`,
        attempt: (attempt) =>
          attempt === 1 ? "Initial generation" : `Retry ${attempt - 1}`,
        reasoning: (attempt) =>
          attempt === 1 ? "Model reasoning" : `Retry ${attempt - 1} reasoning`,
        resultCount: (count) => `${count} result${count === 1 ? "" : "s"}`,
        sizeBytes: (count) => `${count} bytes`,
        validationStages: {
          package_files: "Package file validation started",
        },
        stop: "Stop this turn",
        stopping: "Stopping…",
        stages: {
          request_accepted: "Request received",
          attempt_started: "Model generation started",
          reasoning: "Model reasoning",
          tool_started: "Tool started",
          tool_completed: "Tool completed",
          tool_failed: "Tool failed",
          candidate_generated: "Candidate generated",
          validation_started: "Deterministic validation started",
          validation_passed: "Deterministic validation passed",
          validation_failed: "Deterministic validation failed",
          repair_started: "Repair retry started",
          run_terminal: "Operation finished",
          commit_accepted: "Create request received",
          commit_validation_started: "Candidate validation started",
          commit_validation_passed: "Candidate validation passed",
          commit_persistence_started: "Saving Skill and candidate version",
          commit_persistence_completed: "Skill and candidate version saved",
          commit_terminal: "Create operation finished",
        },
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
        created:
          "The Skill was created in a suspended state. Review and activate it when ready.",
        withVersion: (version) =>
          `Saved Candidate Version v${version}. Go activate it`,
        withoutVersion: "Saved a Candidate Version. Go activate it",
        goActivate: "Go activate",
        viewSkill: "View Skill",
        viewCandidateVersion: "View Candidate Version",
        revisionWithSecrets: (_version, count) =>
          `${count} environment variable${count === 1 ? " needs" : "s need"} runtime secrets.`,
        createdWithSecrets: (count) =>
          `${count} environment variable${count === 1 ? " needs" : "s need"} runtime secrets.`,
        configureSecrets: "Configure secrets",
      },
      versionConflict: {
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
    officialWebsite: "Fluva's official website",
    settingsAndMore: "Settings and more",
    contactUs: "Contact us",
    about: "About Fluva",
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
    modelQuotaExceededTitle: "Model quota exhausted",
    modelQuotaExceededDescription:
      "The configured model quota was exhausted. Increase provider quota or select an available model before retrying.",
    modelAuthenticationFailedTitle: "Model authentication failed",
    modelAuthenticationFailedDescription:
      "The selected model provider rejected authentication. Replace or verify that model configuration's API key before retrying.",
    modelProviderBusyTitle: "Model provider busy",
    modelProviderBusyDescription:
      "The selected model provider remained busy after Worker retries. Retry later or select another available model.",
    modelCircuitOpenTitle: "Model requests temporarily paused",
    modelCircuitOpenDescription:
      "The model request circuit breaker was open after recent provider failures. Wait for recovery or verify the provider before retrying.",
    modelRequestFailedTitle: "Model request failed",
    modelRequestFailedDescription:
      "The model request failed after retry. The available public evidence does not distinguish the provider, proxy, or Worker host network as the direct cause.",
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
    loopSafetyLimitTitle: "Repeated operations triggered the safety limit",
    loopSafetyLimitDescription:
      "This Run has stopped. Any existing answer or files are partial results; the failure reason is the loop safety limit.",
    loopFinalizationFailedTitle: "Loop finalization failed",
    loopFinalizationFailedDescription:
      "After the repeated-call limit fired, the model attempted another tool call or returned no visible tool-free answer, so the Run could not finalize.",
    toolExecutionFailedTitle: "Tool execution failed",
    toolExecutionFailedDescription:
      "A tool execution returned an explicit failure before the Run could complete. Tool arguments and private output are not shown in this status.",
    runPolicyStaleTitle: "Run policy unavailable",
    runPolicyStaleDescription:
      "The Run Snapshot's frozen runtime policy could not be materialized, so execution failed closed. Validate the active policy and start a new Run.",
    contextCapacityExceededTitle: "Context exceeds the model capacity",
    contextCapacityExceededDescription:
      "The frozen request still could not fit after automatic compaction. Shorten the conversation, reduce fixed instructions or tools, or select a model with a larger context window before starting a new Run.",
    contextProviderCallAmbiguousTitle:
      "Provider call outcome could not be confirmed",
    contextProviderCallAmbiguousDescription:
      "The Provider may already have processed this request, so it must not be replayed automatically. Review the conversation state before starting distinct follow-up work.",
    toolCallControlStateInvalidTitle: "Tool-call control state invalid",
    toolCallControlStateInvalidDescription:
      "The checkpointed tool-call control state did not match the frozen policy or execution scope, so the Run failed closed.",
    toolCallControl: {
      progressLabel: "Run control progress",
      repeatedWarningTitle: "Repeated tool-call pattern detected",
      repeatedWarningDescription: (count, hardLimit) =>
        `The same tool-call set has appeared ${count} times (limit ${hardLimit}). The Agent can change strategy or finish from existing evidence.`,
      repeatedLimitTitle: "Repeated-call limit reached",
      repeatedLimitDescription:
        "The repeated batch was rejected. The Agent has one tool-free finalization attempt to preserve a useful partial result.",
      toolBudgetWarningTitle: (toolName) =>
        `${toolName} is nearing its call limit`,
      toolBudgetWarningDescription: (count, hardLimit) =>
        `${count} of ${hardLimit} admitted calls have been used. Remaining calls are reserved for material gaps.`,
      toolBudgetExhaustedTitle: "Run internal tool-call limit reached",
      toolBudgetExhaustedDescription:
        "No new internal tool calls can be admitted in this Run. The Agent can finish with the evidence already collected.",
      leadToolBudgetExhaustedTitle: "Lead Agent tool-call limit reached",
      leadToolBudgetExhaustedDescription:
        "No new internal tool calls can be admitted for the Lead Agent. Each active Sub-Agent Task keeps its own independent allowance.",
      subagentTaskToolBudgetExhaustedTitle:
        "Sub-Agent Task tool-call limit reached",
      subagentTaskToolBudgetExhaustedDescription:
        "No new internal tool calls can be admitted for this Sub-Agent Task. The Lead Agent and other Sub-Agent Tasks are unaffected.",
      subagentTotalLimitTitle: "Sub-Agent delegation limit reached",
      subagentTotalLimitDescription:
        "No more Sub-Agent Tasks can be admitted in this Run. The Lead Agent can continue with the results already collected.",
    },
    tokenBudgetReachedTitle: "Run token budget reached",
    tokenBudgetReachedDescription:
      "This run stopped early, so the response may be incomplete.",
    outputDeliveryIncompleteTitle: "Output file was not delivered",
    outputDeliveryIncompleteDescription:
      "A required output file was created but was not published to this conversation. Resending may repeat an already completed command, so review the run first.",
    sideEffectStateUnknownTitle: "Run state could not be confirmed",
    sideEffectStateUnknownDescription:
      "Some operations may already have completed, but the Worker could not confirm the final state. To avoid repeating them, do not resend this message directly; review the run first.",
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
      "The Agent's configured model could not be resolved. Check its system binding, Definition, and active model catalog entry, then retry.",
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
      telegram: "Telegram direct messages through your Fluva bot.",
      slack: "Slack workspace messages and mentions.",
      discord: "Discord server messages through your Fluva bot.",
      feishu: "Feishu and Lark messages through your Fluva app.",
      dingtalk: "DingTalk Stream Push messages through your Fluva bot.",
      wechat: "WeChat iLink messages through your Fluva bot.",
      wecom: "WeCom messages through your Fluva AI bot.",
    },
    connectedAs: (name: string) => `Connected as ${name}.`,
  },

  // Page titles (document title)
  pages: {
    appName: "Fluva",
    chats: "Chats",
    newChat: "New chat",
    untitled: "Untitled",
  },

  // Tool calls
  toolCalls: {
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
    skillInstallTooltip: "Install skill and make it available to Fluva",
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
    title: "Request to run a command in the Fluva Worker host environment.",
    localHost: "Local host",
    riskTitle: "Runs with your local account permissions",
    riskWarning:
      "This is not an isolated sandbox. The command runs in the Fluva Worker OS environment and may access files and credentials available to your local account, use the network, start background processes, and return output to the Agent or model. Allow it only if you trust this exact command.",
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
      "The command or one of its child processes may still be running and producing side effects. Inspect the Fluva Worker host environment before requesting it again; this authorization cannot be retried.",
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
    unknown: "Subtask status pending",
    in_progress: "Running subtask",
    completed: "Subtask completed",
    failed: "Subtask failed",
    stopReasons: {
      token_capped: "The Sub-Agent reached its token budget.",
      turn_capped: "The Sub-Agent reached its turn budget.",
      loop_capped: "The Sub-Agent stopped after a repeated tool-call loop.",
      tool_budget_capped:
        "The Sub-Agent reached its tool-call budget; any usable result was preserved.",
      output_truncated:
        "The Provider truncated the Sub-Agent output; the partial result may be incomplete.",
    },
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
    title: "Context Usage",
    close: "Close context usage",
    full: (percent: string) => `${percent} Full`,
    usage: "Estimated context usage",
    loading: "Measuring the current context…",
    unavailable: "Current context usage is unavailable.",
    disabled: "Off",
    progressLabel: (percent: string) => `Estimated context usage ${percent}`,
    usageWithoutCapacity: (estimated: string) =>
      `Approximately ${estimated} context; window capacity is unknown`,
    lowerBoundUsage: (lowerBound: string) => `At least ${lowerBound} context`,
    capacityUnavailable:
      "The model has no configured context window limit, so usage cannot be calculated.",
    capacityUnknown: "Model context capacity is unknown",
    contextWindowLimit: "Total context",
    notConfigured: "Not configured",
    safetyBound: "Safe occupancy bound",
    previousProviderInput: "Last Provider input",
    compactionThreshold: "Automatic compaction line",
    stale: "Data is stale",
    unmeasuredVisuals: (count: number) =>
      `${count} additional image${count === 1 ? " is" : "s are"} not yet measured`,
    lanes: {
      system_prompt: "System prompt",
      agent_instructions: "Agent instructions",
      tool_definitions: "Tool definitions",
      skills: "Skills",
      mcp_dynamic_tools: "MCP & dynamic tools",
      subagent_definitions: "Sub-Agent definitions",
      summarized_conversation: "Summarized conversation",
      conversation: "Conversation",
      visual_media: "Images & media",
      provider_overhead: "Provider request overhead",
    },
    compressionConditions: "Automatic compression conditions",
    noCompressionConditions: "No automatic compression conditions configured.",
    current: "Current condition value",
    triggerAt: "Compress at",
    remaining: "Until compression",
    triggerStatus: "Status",
    triggerReached: "Trigger condition reached",
    estimatedContext: "Current context",
    tokenThreshold: "Equivalent Token threshold",
    summaryPresent: "Includes the previous compression summary",
    allConditions: "Configured conditions",
    anyCondition: "Compression starts when any condition is reached.",
    primary: "Closest",
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
      "Navigate Fluva faster with keyboard shortcuts.",
    openCommandPalette: "Open Command Palette",
    toggleSidebar: "Toggle Sidebar",
  },

  // Settings
  settings: {
    title: "Settings",
    description: "Adjust how Fluva looks and behaves for you.",
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
        "Control how Fluva collects, retains, and uses long-term memory for your account.",
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
        "Connect IM accounts that can send messages to Fluva from outside the browser.",
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
        "Put your agent skill folders under the `/skills/custom` folder under the root folder of Fluva.",
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
        "This account signs in with {provider}, so Fluva cannot manage or change its password here. Use your SSO provider's account settings instead.",
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
      "Fluva needs an administrator account before new regular accounts can be created.",
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
