import type { LucideIcon } from "lucide-react";

export interface Translations {
  // Locale meta
  locale: {
    localName: string;
  };

  // Common
  common: {
    skipToContent: string;
    home: string;
    settings: string;
    delete: string;
    edit: string;
    rename: string;
    share: string;
    openInNewWindow: string;
    close: string;
    closeArtifacts: string;
    more: string;
    search: string;
    loadMore: string;
    reload: string;
    retry: string;
    historyLoadFailed: string;
    download: string;
    file: string;
    thinking: string;
    thinkingProcess: string;
    thinkingInProgress: (seconds?: number) => string;
    thoughtFor: (seconds?: number) => string;
    artifacts: string;
    public: string;
    custom: string;
    notAvailableInDemoMode: string;
    loading: string;
    version: string;
    lastUpdated: string;
    code: string;
    preview: string;
    cancel: string;
    save: string;
    install: string;
    create: string;
    import: string;
    export: string;
    exportAsMarkdown: string;
    exportAsJSON: string;
    exportSuccess: string;
    regenerate: string;
    editAndRerun: string;
    updateAndRerun: string;
    editRerunWarning: string;
    branch: string;
    showArtifacts: string;
    feedbackHelpful: string;
    feedbackNotHelpful: string;
    feedbackSaveFailed: string;
  };

  runDuration: {
    working: string;
    completedIn: (duration: string) => string;
    description: string;
    lessThanSecond: string;
    hours: (value: number) => string;
    minutes: (value: number) => string;
    seconds: (value: number) => string;
    separator: string;
  };

  home: {
    docs: string;
  };

  // Welcome
  welcome: {
    greeting: string;
    createYourOwnSkill: string;
    createYourOwnSkillDescription: string;
  };

  // Clipboard
  clipboard: {
    copyToClipboard: string;
    copiedToClipboard: string;
    failedToCopyToClipboard: string;
    linkCopied: string;
  };

  // Citations
  citations: {
    sourcesSummary: (count: number) => string;
    citeCount: (count: number) => string;
    copyReference: (title: string) => string;
    copiedReference: (title: string) => string;
  };

  // Workspace Changes
  workspaceChanges: {
    title: string;
    editedTitle: (count: number) => string;
    badge: (
      count: number,
      additions: number | null,
      deletions: number | null,
    ) => string;
    viewChanges: string;
    created: string;
    modified: string;
    deleted: string;
    openFile: string;
    loading: string;
    noChanges: string;
    diffUnavailable: string;
    binaryUnavailable: string;
    largeUnavailable: string;
    sensitiveUnavailable: string;
    truncatedUnavailable: string;
    truncatedSummary: string;
  };

  // Input Box
  inputBox: {
    placeholder: string;
    createSkillPrompt: string;
    addAttachments: string;
    inputPolish: string;
    inputPolishing: string;
    inputPolishNoChanges: string;
    inputPolishFailed: string;
    inputPolishUndo: string;
    inputPolishCancel: string;
    voiceInputStartLabel: string;
    voiceInputStopLabel: string;
    voiceInputStart: string;
    voiceInputListening: string;
    voiceInputUnsupported: string;
    voiceInputPermissionDenied: string;
    voiceInputMicrophoneUnavailable: string;
    voiceInputUnsupportedLanguage: string;
    voiceInputNetworkError: string;
    voiceInputNoSpeech: string;
    voiceInputFailed: string;
    model: string;
    agentModelLocked: string;
    mode: string;
    flashMode: string;
    flashModeDescription: string;
    reasoningMode: string;
    reasoningModeDescription: string;
    proMode: string;
    proModeDescription: string;
    ultraMode: string;
    ultraModeDescription: string;
    researchWorkload: string;
    researchWorkloadDescription: string;
    searchModels: string;
    surpriseMe: string;
    surpriseMePrompt: string;
    followupLoading: string;
    followupConfirmTitle: string;
    followupConfirmDescription: string;
    followupConfirmAppend: string;
    followupConfirmReplace: string;
    suggestionPlaceholderRequired: string;
    goalCommandDescription: string;
    compactCommandDescription: string;
    dreamCommandDescription: string;
    dreamLogCommandDescription: string;
    dreamRestoreCommandDescription: string;
    goalLabel: string;
    goalContinuing: string;
    goalContinuationTooltip: string;
    goalSet: string;
    goalCleared: string;
    goalNone: string;
    goalActive: string;
    goalFailed: string;
    compactSuccess: string;
    compactSkipped: string;
    compactFailed: string;
    compactSourceTooLarge: string;
    compactPromptBudgetTooSmall: string;
    dreamQueued: string;
    dreamAlreadyRunning: string;
    dreamNothingPending: string;
    dreamInvalidArguments: string;
    dreamLogInvalidArguments: string;
    dreamRestoreInvalidArguments: string;
    dreamAttachmentsUnsupported: string;
    dreamFailed: string;
    dreamModelUnavailable: string;
    dreamRequiresThread: string;
    dreamPreparationStarted: string;
    dreamPreparationQueued: string;
    dreamPreparationRunning: string;
    dreamPreparationVerifying: string;
    dreamPreparationCompleted: string;
    dreamPreparationCancelled: string;
    dreamPreparationFailed: string;
    dreamPreparationPasses: string;
    dreamPreparationCancel: string;
    dreamPreparationCancelRequested: string;
    dreamRouteUnavailable: string;
    dreamRestoreSuccess: string;
    dreamRestoreFailed: string;
    dreamRestoreConfirmTitle: string;
    dreamRestoreConfirmDescription: string;
    dreamRestoreConfirmAction: string;
    suggestions: {
      suggestion: string;
      prompt: string;
      icon: LucideIcon;
    }[];
    suggestionsCreate: (
      | {
          suggestion: string;
          prompt: string;
          icon: LucideIcon;
        }
      | {
          type: "separator";
        }
    )[];
    pleaseWaitStreaming: string;
  };

  // Sidebar
  sidebar: {
    recentChats: string;
    newChat: string;
    chats: string;
    demoChats: string;
    agents: string;
    scheduledTasks: string;
    agentsDisabledTooltip: string;
    channels: string;
  };

  project: {
    audit: string;
    automations: string;
    agents: string;
    skills: string;
    mcp: string;
    memory: string;
    usage: string;
    navigation: {
      label: string;
      menuLabel: string;
      workspaceAria: string;
      overview: string;
      conversations: string;
      automations: string;
      agents: string;
      skills: string;
      mcp: string;
      memory: string;
      connections: string;
      audit: string;
      settings: string;
      backToWorkspace: string;
      sections: {
        work: string;
        capabilities: string;
        management: string;
      };
      account: string;
      platformAdministration: string;
      systemSettings: string;
      logout: string;
      expand: string;
      collapse: string;
      open: string;
      sheetTitle: string;
      sheetDescription: string;
    };
    accessDenied: {
      title: string;
      description: (area: string) => string;
      backToProject: string;
    };
    settings: {
      accessArea: string;
      governanceEyebrow: (projectName: string) => string;
      title: string;
      description: string;
      navigationLabel: string;
      navigation: {
        general: { label: string; description: string };
        members: { label: string; description: string };
      };
      general: {
        title: string;
        description: string;
        displayName: string;
        icon: string;
        slug: string;
        slugDescription: string;
        projectDescription: string;
        descriptionPlaceholder: string;
        saved: string;
        saving: string;
        save: string;
      };
      lifecycle: {
        title: string;
        description: string;
        requestDeletion: string;
        confirmTitle: string;
        confirmDescription: string;
        cancel: string;
        confirm: string;
      };
    };
    members: {
      managementEyebrow: (projectName: string) => string;
      title: string;
      description: string;
      inviteMember: string;
      membersTitle: string;
      invitationsTitle: string;
      emptyInvitations: string;
      columns: {
        account: string;
        role: string;
        status: string;
        actions: string;
      };
      roles: {
        admin: string;
        editor: string;
        runner: string;
        viewer: string;
      };
      membershipStatuses: {
        active: string;
        left: string;
        removed: string;
      };
      invitationStatuses: {
        pending: string;
        redeemed: string;
        revoked: string;
        expired: string;
      };
      actions: {
        changeRole: string;
        removeMember: string;
        leaveProject: string;
        revokeInvitation: string;
      };
      confirmations: {
        removeTitle: string;
        removeDescription: (email: string) => string;
        removeConfirm: string;
        leaveTitle: string;
        leaveDescription: string;
        leaveConfirm: string;
        revokeTitle: string;
        revokeDescription: (email: string) => string;
        revokeConfirm: string;
        cancel: string;
      };
      inviteDialog: {
        title: string;
        description: string;
        inviteLink: string;
        done: string;
        email: string;
        projectRole: string;
        create: string;
      };
      roleDialog: {
        title: string;
        projectRole: string;
        save: string;
      };
    };
    governance: {
      retry: string;
      tokenSeries: {
        title: string;
        description: string;
        loading: string;
        unavailableTitle: string;
        unavailableDescription: string;
        emptyTitle: string;
        emptyDescription: string;
        window: string;
        settlementNote: string;
        interactionHint: string;
        chartLabel: string;
        tableCaption: string;
        bucket: string;
      };
      usage: {
        title: string;
        description: string;
        settingsDescription: string;
        loading: string;
        unavailableTitle: string;
        unavailableDescription: string;
        thresholdReached: string;
        used: string;
        reserved: string;
        limit: string;
        tightenTitle: string;
        updateError: string;
        updateConflict: string;
        updateUnavailable: string;
        platformLimitRule: string;
        platformLimitExceeded: (dimension: string, limit: string) => string;
        saving: string;
        save: string;
        dimensions: {
          members: string;
          storage_bytes: string;
          concurrent_runs: string;
          mcp_calls_daily: string;
        };
      };
      audit: {
        title: string;
        description: string;
        loading: string;
        unavailableTitle: string;
        unavailableDescription: string;
        emptyTitle: string;
        emptyDescription: string;
        olderEvents: string;
        previousPage: string;
        nextPage: string;
        page: (page: number) => string;
        pageSize: string;
        pageSizeOption: (pageSize: number) => string;
        itemsOnPage: (count: number) => string;
        columns: {
          time: string;
          action: string;
          outcome: string;
          actor: string;
          target: string;
          details: string;
          error: string;
        };
      };
    };
  };

  projectMemory: {
    title: string;
    description: string;
    currentTab: string;
    archiveTab: string;
    documentFileName: string;
    mediaType: string;
    version: (value: number) => string;
    updated: string;
    neverUpdated: string;
    viewMode: string;
    source: string;
    preview: string;
    viewChanges: string;
    versionsTitle: string;
    versionsDescription: string;
    versionsFailed: string;
    noVersions: string;
    previous: string;
    next: string;
    reviewTitle: string;
    reviewDescription: string;
    reviewAction: string;
    emptyTitle: string;
    emptyDescription: string;
    pending: string;
    pendingUnit: (value: number) => string;
    dream: string;
    dreaming: string;
    dreamRunning: string;
    dreamUnavailable: string;
    dreamQueuedBudget: string;
    dreamQueuedItems: (value: number) => string;
    dreamAlreadyRunning: string;
    dreamNothingPending: string;
    dreamFailed: string;
    dreamModelUnavailable: string;
    restoreSucceeded: (version: number) => string;
    restoreFailed: string;
    autoDream: string;
    manualDream: string;
    restoreTrigger: string;
    budgetRewrite: string;
    handled: (value: number) => string;
    changed: string;
    unchanged: string;
    needsReview: string;
    overBudgetTitle: string;
    overBudgetDescription: string;
    injectionInactiveTitle: string;
    injectionPlatformDisabledDescription: string;
    injectionAccountDisabledDescription: string;
    compressNow: string;
    detailsTitle: (value: number) => string;
    detailsDescription: string;
    diffTitle: string;
    diffTruncatedTitle: string;
    diffTruncatedDescription: string;
    documentTitle: string;
    noDiff: string;
    restore: string;
    restoring: string;
    restoreTitle: (value: number) => string;
    restoreDescription: string;
    cancel: string;
    confirmRestore: string;
    loadFailed: string;
    detailFailed: string;
    retry: string;
    archiveDescription: string;
    searchPlaceholder: string;
    search: string;
    archiveFailed: string;
    archiveEmpty: string;
    archiveNoMatch: string;
    loadMore: string;
    loadingMore: string;
    originSnip: string;
    originRemember: string;
    pendingTitle: string;
    pendingDescription: string;
    pendingFailed: string;
    tags: {
      permanent: string;
      durable: string;
      ephemeral: string;
      correction: string;
    };
  };

  adminOperations: {
    shellTitle: string;
    shellDescription: string;
    signOut: string;
    retry: string;
    gatewayUnavailable: {
      title: string;
      description: string;
      reload: string;
    };
    ui: {
      navigationGroups: {
        operations: string;
        governance: string;
      };
      skipToContent: string;
      close: string;
      backToWorkspace: string;
      expandNavigation: string;
      collapseNavigation: string;
      previousPage: string;
      nextPage: string;
      page: (page: number) => string;
      pageSize: string;
      pageSizeOption: (pageSize: number) => string;
      itemsOnPage: (count: number) => string;
      copy: string;
      copied: string;
      platformHealthy: string;
      publicErrorCode: string;
      eventId: string;
      jobId: string;
      clearFilters: string;
    };
    navigation: {
      label: string;
      overview: string;
      projects: string;
      jobs: string;
      audit: string;
      assets: string;
      systemSettings: string;
      settings: string;
    };
    overview: {
      title: string;
      loading: string;
      unavailableTitle: string;
      unavailableDescription: string;
      readiness: {
        title: string;
        workerCount: string;
        workerCapacity: string;
        oldestHeartbeat: string;
        schedulerOwnership: string;
        secondsAgo: string;
        notReported: string;
        states: {
          ready: string;
          degraded: string;
          closed: string;
          unavailable: string;
          disabled: string;
          polling: string;
          owned: string;
          unowned: string;
          ownership_lost: string;
          unknown: string;
        };
        components: {
          database: string;
          schema: string;
          worker_fleet: string;
          scheduler: string;
          stream: string;
          quota: string;
          audit: string;
        };
      };
      channels: {
        title: string;
        emptyTitle: string;
        empty: string;
      };
      counts: {
        projects: string;
        suspendedProjects: string;
        queuedJobs: string;
        runningJobs: string;
        deadJobs: string;
      };
      usage: {
        title: string;
        members: string;
        storage_bytes: string;
        concurrent_runs: string;
        mcp_calls_daily: string;
        used: string;
        reserved: string;
      };
    };
    projects: {
      title: string;
      loading: string;
      unavailableTitle: string;
      unavailableDescription: string;
      emptyTitle: string;
      emptyDescription: string;
      older: string;
      suspended: string;
      active: string;
      pendingDeletion: string;
      details: string;
      fields: {
        projectId: string;
        slug: string;
        createdAt: string;
        updatedAt: string;
        deletionAt: string;
      };
      filters: {
        query: string;
        queryPlaceholder: string;
        status: string;
        suspension: string;
        notSuspended: string;
        all: string;
        apply: string;
        clear: string;
        invalid: string;
      };
      actions: {
        governAssets: string;
        suspend: string;
        resume: string;
        pending: string;
        error: string;
        confirmSuspendTitle: string;
        confirmSuspendDescription: string;
        confirmResumeTitle: string;
        confirmResumeDescription: string;
        cancel: string;
        confirm: string;
      };
    };
    jobs: {
      title: string;
      loading: string;
      unavailableTitle: string;
      unavailableDescription: string;
      emptyTitle: string;
      emptyDescription: string;
      older: string;
      requeue: string;
      requeueing: string;
      requeueError: string;
      copyProjectId: string;
      projectIdCopied: string;
      statuses: {
        queued: string;
        leased: string;
        running: string;
        retry_wait: string;
        succeeded: string;
        failed: string;
        cancelled: string;
        dead: string;
      };
      types: {
        private_run: string;
        automation_run: string;
        retention_purge: string;
        mcp_discovery: string;
        memory_dream: string;
        memory_dream_prepare: string;
        memory_seal: string;
      };
      retrySafetyLabel: string;
      errorLabel: string;
      retrySafety: {
        safe: string;
        unsafe: string;
        unknown: string;
      };
      filters: {
        label: string;
        project: string;
        allProjects: string;
        status: string;
        type: string;
        allStatuses: string;
        allTypes: string;
        apply: string;
        clear: string;
      };
    };
    audit: {
      title: string;
      loading: string;
      unavailableTitle: string;
      unavailableDescription: string;
      emptyTitle: string;
      emptyDescription: string;
      older: string;
      columns: {
        time: string;
        action: string;
        outcome: string;
        actor: string;
        target: string;
        project: string;
        error: string;
      };
      filters: {
        label: string;
        project: string;
        allProjects: string;
        platformOnly: string;
        apply: string;
        clear: string;
      };
    };
  };

  adminAssets: {
    navigation: {
      platformLabel: string;
      projectLabel: string;
      agent: string;
      skill: string;
      mcp: string;
      quota: string;
    };
    shell: {
      platformAria: string;
      systemCatalog: string;
      adminScope: string;
      projectAria: string;
      backToProjects: string;
      projectGovernance: string;
      projectId: string;
      shortProjectId: (projectId: string) => string;
      projectIdentityUnavailable: string;
    };
    common: {
      assetVersion: string;
      versionHistory: string;
      retry: string;
      retrying: string;
    };
    status: {
      active: string;
      archived: string;
      suspended: string;
      draft: string;
      pending_approval: string;
      published: string;
      rejected: string;
      retired: string;
      revoked: string;
    };
    mcpToolInventory: {
      title: string;
      description: string;
      toolCount: (count: number) => string;
      loading: string;
      unpublished: string;
      neverDiscovered: string;
      testing: string;
      catalogInvalid: string;
      discoveryUnavailable: string;
      stale: string;
      refreshFailed: string;
      degradedSuffix: string;
      empty: string;
      lastSuccess: string;
      noDescription: string;
      testService: string;
      retestService: string;
      testingAction: string;
      testFailurePrefix: string;
      loadErrors: {
        notFound: string;
        forbidden: string;
        authRequired: string;
        responseInvalid: string;
        network: string;
        generic: string;
      };
      testErrors: {
        notFound: string;
        forbidden: string;
        authRequired: string;
        conflict: string;
        network: string;
        generic: string;
      };
    };
    catalog: {
      assetCatalog: string;
      projectAgentTitle: string;
      projectSkillTitle: string;
      projectMcpTitle: string;
      systemAssets: string;
      systemAssetsDescription: string;
      systemCurrentAssetsDescription: string;
      systemMcpDescription: string;
      searchPlaceholder: string;
      filterAll: string;
      catalogReady: string;
      totalAssets: string;
      activeAssets: string;
      unpublishedAssets: string;
      assetsWithoutCurrentVersion: string;
      latestUpdate: string;
      noUpdate: string;
      publicationFilter: string;
      publicationAll: string;
      publishedOnly: string;
      unpublishedOnly: string;
      updatedSort: string;
      newestFirst: string;
      oldestFirst: string;
      identifier: string;
      source: string;
      systemCatalogSource: string;
      lifecycleStatus: string;
      publicationStatus: string;
      published: string;
      currentVersionStatus: string;
      currentVersionAvailable: string;
      currentVersionMissing: string;
      assetRevision: string;
      actions: string;
      viewDetails: string;
      refresh: string;
      refreshing: string;
      resultRange: (from: number, to: number, total: number) => string;
      page: (page: number, totalPages: number) => string;
      previousPage: string;
      nextPage: string;
      noResults: string;
      catalogUnavailable: string;
      versionHistoryUnavailable: string;
      projectCatalogUnavailable: string;
      noSystemAssets: string;
      system: string;
      systemPublishStatus: string;
      pinnedVersion: string;
      bindingStatus: string;
      bindingRevision: string;
      publishedAvailable: string;
      unpublished: string;
      enabledAndPinned: string;
      closed: string;
      notBound: string;
      enabled: string;
      none: string;
      manageBinding: string;
      projectAssets: string;
      projectAgentDescription: string;
      projectVersionedDescription: string;
      noProjectAssets: string;
      project: string;
      publishStatus: string;
      createNewVersion: string;
      waitingForAdmin: string;
      archive: string;
      activate: string;
      disable: string;
      suspend: string;
    };
    version: {
      current: string;
      candidate: string;
      historical: string;
      currentUnconfirmed: string;
      activate: string;
      none: string;
      mcpNone: string;
      selectHint: string;
      number: (number: number) => string;
      mcpNumber: (number: number) => string;
      publish: string;
      publishMcp: string;
      submit: string;
      approve: string;
      approveMcp: string;
    };
    diff: {
      payloadChecksum: string;
      description: string;
      model: string;
      toolGroups: string;
      skillVersions: string;
      mcpVersions: string;
      compatibility: string;
      scanDecision: string;
      scanAllow: string;
      scanWarn: string;
      scanBlock: string;
      scanRules: string;
      files: string;
      secretRequirements: string;
      transport: string;
      command: string;
      url: string;
      arguments: string;
      timeout: string;
      secretSlots: string;
      status: string;
      payloadSchemaVersion: string;
      payloadFields: string;
      optional: string;
      required: string;
      noDescription: string;
      seconds: (seconds: number) => string;
      noChanges: string;
      field: string;
      previous: string;
      current: string;
      previousMcpConfiguration: string;
      currentMcpConfiguration: string;
    };
    runtime: {
      unsupportedProjectTransport: string;
      unsupportedSystemTransport: string;
      missingProjectUrl: string;
      invalidProjectUrl: string;
      projectOAuth: string;
      projectCredentialTargetsOnly: string;
      missingSystemCommand: string;
      missingSystemUrl: string;
      systemEnvOnly: string;
      systemRemoteSecretsOnly: string;
    };
    dialogs: {
      authoring: {
        title: (name: string) => string;
        skillDescription: string;
        mcpDescription: string;
        description: string;
        secretSlots: string;
        invalidSecretSlots: string;
        cancel: string;
        saving: string;
        save: string;
      };
      binding: {
        switchTitle: string;
        enableTitle: string;
        description: (name: string) => string;
        selectPublished: string;
        selectPublishedAria: string;
        selectPlaceholder: string;
        unavailableSuffix: string;
        noBindableVersions: string;
        currentProject: (version: string) => string;
        notEnabled: string;
        disable: string;
        enable: string;
        rollback: string;
        switchVersion: string;
        currentVersion: string;
        currentVersionUnavailable: string;
        currentVersionInUse: string;
      };
    };
    errors: {
      notFound: string;
      forbidden: string;
      conflict: string;
      validationFailed: string;
      mcpVersionValidation: string;
      storageQuota: string;
      storageUnavailable: string;
      authRequired: string;
      network: string;
      invalidResponse: string;
      invalidErrorResponse: string;
      fallback: string;
    };
  };

  adminSystemSettings: {
    header: {
      eyebrow: string;
      title: string;
      refresh: string;
      refreshing: string;
    };
    states: {
      loading: string;
      unavailableTitle: string;
      unavailableDescription: string;
      retry: string;
    };
    sections: {
      auth: { title: string; description: string };
      automations: { title: string; description: string };
      quotas: { title: string; description: string };
      agentRuntime: { title: string; description: string };
      memoryDocument: { title: string; description: string };
    };
    groups: {
      runLimits: string;
      assistantExperience: string;
      summarization: string;
      memory: string;
      tools: string;
      safeguards: string;
    };
    fields: {
      allowRegistration: string;
      defaultMemberLimit: string;
      defaultStorageLimit: string;
      defaultConcurrentRuns: string;
      defaultDailyMcpCalls: string;
      quotaWarningThreshold: string;
      defaultModel: string;
      unavailableModel: string;
      addRow: string;
      removeRow: string;
      listHint: string;
      memoryDocumentSections: string;
      memoryDocumentSectionsHint: string;
      memoryDocumentSectionInput: (position: number) => string;
      addMemoryDocumentSection: string;
      removeMemoryDocumentSection: (position: number) => string;
      moveMemoryDocumentSectionUp: (position: number) => string;
      moveMemoryDocumentSectionDown: (position: number) => string;
    };
    common: {
      save: string;
      saving: string;
      reset: string;
      revision: (revision: number) => string;
      effectiveRevision: (revision: number) => string;
      updatedAt: (value: string) => string;
      storedRevision: (revision: number) => string;
      pendingRoles: (roles: string) => string;
      noPendingRoles: string;
    };
    effects: {
      newRequests: string;
      newRuns: string;
      newRequestsAndRuns: string;
      newMemoryDocuments: string;
      nextAuthoritativeCheck: string;
      restartRequired: string;
    };
    feedback: {
      saved: string;
      registrationConfirmation: string;
      conflict: string;
      invalid: string;
      inactiveModel: string;
      authRequired: string;
      generic: string;
    };
  };

  automation: {
    title: string;
    create: string;
    editTitle: string;
    deleteTitle: string;
    emptyTitle: string;
    emptyDescription: string;
    filterEmptyTitle: string;
    selectPrompt: string;
    runNow: string;
    schedulerDisabled: string;
    migrationRequired: string;
    retry: string;
    history: string;
    fields: {
      title: string;
      prompt: string;
      schedule: string;
    };
  };

  // Scheduled tasks
  scheduledTasks: {
    description: string;
    migrationComplete: {
      title: string;
      description: string;
    };
    scheduleType: { cron: string; once: string };
    preset: {
      label: string;
      hourly: string;
      daily: string;
      weekly: string;
      monthly: string;
      custom: string;
    };
    fields: {
      minute: string;
      time: string;
      weekday: string;
      dayOfMonth: string;
      cron: string;
      cronPlaceholder: string;
      runAt: string;
      timezone: string;
    };
    weekdays: {
      mon: string;
      tue: string;
      wed: string;
      thu: string;
      fri: string;
      sat: string;
      sun: string;
    };
    preview: string;
    cronHelp: string;
    create: {
      title: string;
      taskTitle: string;
      prompt: string;
      submit: string;
      fillRequired: string;
    };
    context: {
      fresh: string;
      reuse: string;
      threadIdPlaceholder: string;
    };
    filters: {
      status: string;
      type: string;
      all: string;
      allStatuses: string;
      enabled: string;
      paused: string;
      completed: string;
      failed: string;
      allTypes: string;
      cron: string;
      once: string;
    };
    empty: {
      title: string;
      description: string;
      action: string;
      filteredTitle: string;
      filteredDescription: string;
      clearFilters: string;
    };
    detail: {
      contextMode: string;
      thread: string;
      lastThread: string;
      schedule: string;
      nextRun: string;
      lastRun: string;
      lastRunId: string;
      lastError: string;
      runCount: string;
      runsCount: string;
      runsCountOne: string;
      noRuns: string;
      noSelection: string;
      filteredByThread: string;
      loadFailed: string;
    };
    actions: {
      edit: string;
      cancelEdit: string;
      pause: string;
      resume: string;
      trigger: string;
      delete: string;
    };
    deleteConfirm: string;
    errors: {
      create: string;
      update: string;
      pause: string;
      resume: string;
      trigger: string;
      delete: string;
    };
    edit: {
      titlePlaceholder: string;
      promptPlaceholder: string;
      submit: string;
    };
    status: {
      enabled: string;
      paused: string;
      running: string;
      completed: string;
      failed: string;
      cancelled: string;
    };
    runTrigger: { scheduled: string; manual: string };
    runStatus: {
      queued: string;
      running: string;
      success: string;
      failed: string;
      skipped: string;
      interrupted: string;
    };
    recipes: {
      label: string;
      trending: { title: string; desc: string };
      news: { title: string; desc: string };
      issues: { title: string; desc: string };
      weekly: { title: string; desc: string };
    };
  };

  // Agents
  agents: {
    common: {
      cancel: string;
      close: string;
      retry: string;
      retrying: string;
      send: string;
      system: string;
      project: string;
      defaultSuffix: string;
      count: (value: number) => string;
    };
    builder: {
      errors: {
        unavailable: string;
        conflict: string;
        forbidden: string;
        notFound: string;
        validationFailed: string;
        invalidResponse: string;
        network: string;
        slugConflict: string;
        unresolvedConflict: string;
        sessionLimitExceeded: string;
        commitUncertain: string;
        stale: string;
      };
      start: {
        title: string;
        hint: string;
        nameLabel: string;
        placeholder: string;
        savedAs: (value: string) => string;
        creating: string;
        continue: string;
        forbidden: string;
        nameTooShort: string;
        nameTooLong: string;
        nameInvalid: string;
      };
      progress: {
        stepsAria: string;
        designing: string;
        steps: string;
      };
      resume: {
        title: string;
        lastUpdated: (value: string) => string;
        deleteAria: (name: string) => string;
        deleteTitle: string;
        deleteDescription: (name: string) => string;
        deleting: string;
        confirmDelete: string;
      };
      blueprint: {
        title: string;
        triggerLabel: string;
        openAria: string;
        conflictCount: (count: number) => string;
        readyTitle: string;
        summary: (conflictCount: number) => string;
        viewBlueprint: string;
        closeAria: string;
        panelSummary: (conflictCount: number) => string;
        tabsAria: string;
        overviewTab: string;
        documentsTab: string;
        runtime: string;
        noDescription: string;
        nameLabel: string;
        savedAs: (value: string) => string;
        model: string;
        capabilities: string;
        dependencySummary: (
          toolGroups: number,
          skills: number,
          mcps: number,
        ) => string;
        checkingMcp: string;
        modelUnavailable: string;
        modelRecovery: string;
        assumptionsTitle: string;
        conflictsTitle: string;
        conflictDocuments: string;
        openConflictDocument: (name: string) => string;
        blockingConflictHint: string;
        createHint: string;
        creating: string;
        createAgent: string;
        validation: {
          descriptionRequired: string;
          modelRequired: string;
          toolGroupRequired: string;
          documentRequired: (name: string) => string;
        };
      };
      conversation: {
        permissionReadOnly: string;
        creatingAgent: string;
        designingAgent: string;
        composerAria: string;
        saveLocalChangesFirst: string;
        answerQuestionFirst: string;
        generatingBlueprint: string;
        composerPlaceholder: string;
        loadingModels: string;
        modelLoadFailed: string;
        noModels: string;
        selectModelAria: string;
        selectModel: string;
        modelLabel: string;
        backToAgents: string;
        designAgent: string;
        completedRecord: string;
        more: string;
        abandon: string;
        conversationAria: string;
        sessionUnavailable: string;
        abandonTitle: string;
        abandonDescription: string;
        continueDesign: string;
        abandoning: string;
        confirmAbandon: string;
        discardTitle: string;
        discardDescription: string;
        continueEditing: string;
        discardAndLeave: string;
        stopGeneration: string;
        stoppingGeneration: string;
        viewAgent: string;
        activity: {
          title: string;
          reasoning: (value: number) => string;
          duration: (value: number) => string;
          terminal: {
            completed: string;
            failed: string;
            stopped: string;
            cancelled: string;
          };
        };
      };
    };
    instructions: {
      files: {
        agents: string;
        soul: string;
        identity: string;
        user: string;
      };
      sectionAria: string;
      title: string;
      editDescription: string;
      readOnlyDescription: string;
      historicalDescription: string;
      edit: string;
      fixedFiles: string;
      displayMode: string;
      source: string;
      preview: string;
      empty: string;
      editFile: (name: string) => string;
      candidateSaveHint: string;
      discard: string;
      saving: string;
      save: string;
      permissionLost: string;
      recoveryPreserved: string;
      recoverySynced: string;
      recoveryFailed: string;
      recoveryReloading: string;
      invalidResponse: string;
      conflictDetected: string;
      reloadRequired: string;
      reloading: string;
      reload: string;
      discardTitle: string;
      discardDescription: string;
      continueEditing: string;
    };
    capabilities: {
      reasons: {
        archived: string;
        inactive: string;
        bindingDisabled: string;
        bindingMissing: string;
        noCurrentVersion: string;
      };
      remediation: {
        restoreSystemAsset: string;
        enableSystemBinding: string;
        activateCandidateVersion: string;
        activateProjectAsset: string;
      };
      explanationSeparator: string;
      boundCount: (value: number) => string;
      unavailablePrefix: (reason: string) => string;
      remediationPrefix: (reason: string) => string;
      historicalDisabled: string;
      historicalVersion: string;
      historicalVersionDescription: string;
      permissionLost: string;
      recoverySynced: string;
      recoveryPreserved: string;
      recoveryFailed: string;
      recoveryReloading: string;
      conflictDetected: string;
      reloadRequired: string;
      reloading: string;
      permissionBlocked: string;
      preparingCandidate: string;
      catalogLoading: string;
      catalogLoadFailed: string;
      validatingMcp: string;
      mcpValidationFailed: string;
      title: string;
      description: string;
      saving: string;
      saveCandidate: string;
      edit: string;
      builtinGroups: string;
      unchanged: string;
      searchPlaceholder: string;
      searchAria: string;
      catalogLoadingStatus: string;
      catalogLoadFailedStatus: string;
      emptySkills: string;
      emptyMcps: string;
      reload: string;
    };
    catalog: {
      title: string;
      authoringLoadFailed: string;
      authoringLoading: string;
      detailTabsAria: string;
      viewDesignRecord: string;
      instructionsTab: string;
      capabilitiesTab: string;
      viewModeAria: string;
      cards: string;
      list: string;
      chatForbidden: string;
      unavailable: string;
      executeForbidden: string;
      currentVersionRequired: string;
      defaultAdminOnly: string;
      defaultUnavailable: string;
      systemDefaultUnavailable: string;
      mainUnavailable: string;
      mainExecuteForbidden: string;
      mainVersionUnavailable: string;
      emptySystem: string;
      emptyProject: string;
      defaultLoadFailed: string;
      defaultLoading: string;
      defaultUnknown: string;
      setDefaultBlockedAria: (name: string, reason: string) => string;
      setDefaultAria: (name: string) => string;
      settingDefault: string;
      setDefault: string;
      activateAria: (name: string) => string;
      activating: string;
      activate: string;
      chatBlockedAria: (name: string, reason: string) => string;
      chatAria: (name: string) => string;
      creatingChat: string;
      chat: string;
      builtIn: string;
      currentDefault: string;
      suspended: string;
      viewDetails: (name: string) => string;
      mainDescription: string;
      noDescription: string;
      mcpValidationFailed: string;
      systemSection: string;
      systemDescription: string;
      projectSection: string;
      projectDescription: string;
      createChatFailed: string;
      activated: (name: string) => string;
      defaultSet: (name: string) => string;
      mainDefaultSet: string;
    };
    selector: {
      title: string;
      description: string;
      emptyTitle: string;
      emptyDescription: string;
      loading: string;
      loadFailed: string;
      projectAgent: string;
      systemAgent: string;
      enableNow: string;
      enableAndChat: (name: string) => string;
      dependencyTitle: string;
      dependencyDescription: string;
      mcpBlockedTitle: string;
      mcpBlockedDescription: string;
      configure: string;
      createProjectAgent: string;
      contactEditor: string;
      alternateTitle: string;
      alternateDescription: string;
      alternateEmptyTitle: string;
      alternateEmptyDescription: string;
      dependencyLoadFailed: string;
      createChatFailed: string;
      enableFailed: string;
      systemUnavailable: string;
    };
    startContinuation: {
      waitingForService: { title: string; detail: string };
      waitingForAgent: { title: string; detail: string };
      creatingChat: { title: string; detail: string };
      readOnly: { title: string; detail: string };
      error: { title: string; detail: string };
      retryChat: string;
      configuredRetry: string;
      defaultLoadFailed: string;
      dependencyFailed: string;
    };
    newChat: {
      threadName: string;
      defaultUnknown: string;
      mainUnavailable: string;
      projectUnavailable: string;
      loadDefaultFailed: string;
      dependencyFailed: string;
      createFailed: string;
      defaultAdmissionUnavailable: string;
    };
    indicator: {
      unavailable: string;
      label: string;
      startWithOther: (current: string) => string;
      current: (current: string) => string;
    };
  };

  skills: {
    catalog: {
      viewDesignRecord: string;
    };
    export: {
      label: string;
      preparing: string;
      started: string;
    };
    secrets: {
      workbenchAria: string;
      filesTab: string;
      secretsTab: string;
      aria: string;
      title: string;
      viewSource: string;
      checking: string;
      syncing: string;
      recognized: (count: number) => string;
      draftUpdated: string;
      retry: string;
      sourceStale: string;
      invalidDeclaration: string;
      forbidden: string;
      notFound: string;
      invalidResponse: string;
      unavailable: string;
      invalidSource: string;
      openSource: string;
      managedComments: string;
      shorthand: (count: number) => string;
      empty: string;
      beginEdit: string;
      optional: string;
      required: string;
      setOptional: (name: string) => string;
      remove: (name: string) => string;
      addTitle: string;
      nameLabel: string;
      namePlaceholder: string;
      targetEnvLabel: string;
      targetEnvPlaceholder: string;
      newOptional: string;
      add: string;
      invalidName: string;
      invalidTargetEnv: string;
      duplicateName: string;
      duplicateTargetEnv: string;
      autonomousTitle: string;
      autonomousDescription: string;
      autonomousAria: string;
      advancedSettings: string;
      injectionAutomatic: string;
      injectionAutomaticDescription: string;
      injectionExplicit: string;
      injectionExplicitDescription: string;
      location: (line: number, column: number | null) => string;
      loadSourceFailed: string;
      loadSource: string;
      saveBlocked: string;
    };
    activationDialog: {
      title: string;
      description: (version: number) => string;
      loading: string;
      noRequirements: string;
      bindingsTitle: string;
      required: string;
      optional: string;
      statusConfigured: string;
      statusMissing: string;
      statusInvalid: string;
      preflightReady: string;
      preflightBlocked: string;
      preflightSummary: (
        configuredRequired: number,
        required: number,
        invalid: number,
      ) => string;
      configureSecrets: string;
      approvalRequiredForActive: string;
      cancel: string;
      retry: string;
      activating: string;
      activate: string;
    };
    builder: {
      errors: {
        unavailable: string;
        modelUnavailable: string;
        effortUnsupported: string;
        conflict: string;
        forbidden: string;
        notFound: string;
        limitExceeded: string;
        validationFailed: string;
        invalidResponse: string;
        network: string;
        commitUncertain: string;
        stale: string;
        targetDeleted: string;
        targetDeletedBanner: string;
        targetDeletedStatus: string;
        noChanges: string;
        baseStale: string;
        targetSessionExists: string;
        targetUnsupported: string;
        targetConflict: string;
        attachmentTooLarge: string;
        attachmentNotUtf8: (name: string) => string;
        attachmentInvalidName: string;
        attachmentTooMany: (max: number) => string;
        attachmentTotalTooLarge: string;
        packageTooLarge: string;
        fileTooLarge: string;
      };
      start: {
        title: string;
        nameLabel: string;
        placeholder: string;
        savedAs: (value: string) => string;
        creating: string;
        continue: string;
        forbidden: string;
        nameTooShort: string;
        nameTooLong: string;
        nameInvalid: string;
      };
      resume: {
        titleCreate: string;
        titleRevise: string;
        titleMixed: string;
        kindCreate: string;
        kindRevise: string;
        lastUpdated: (value: string) => string;
        deleteAriaCreate: (name: string) => string;
        deleteAriaRevise: (name: string) => string;
        deleteTitleCreate: string;
        deleteTitleRevise: string;
        deleteDescriptionCreate: (name: string) => string;
        deleteDescriptionRevise: (name: string) => string;
        deleting: string;
        confirmDelete: string;
      };
      revision: {
        button: string;
        opening: string;
        saveLocalChangesFirst: string;
      };
      conversation: {
        progressAriaCreate: string;
        progressAriaRevise: string;
        permissionReadOnlyCreate: string;
        permissionReadOnlyRevise: string;
        creatingSkill: string;
        creatingCandidate: string;
        processing: string;
        composerAriaCreate: string;
        composerAriaRevise: string;
        saveLocalChangesFirst: string;
        answerQuestionFirst: string;
        generatingFiles: string;
        placeholderCreate: string;
        placeholderRevise: string;
        send: string;
        fallbackTitle: string;
        revisingBanner: (slug: string, version: number) => string;
        completedRecord: (version: number | null) => string;
        unsavedChanges: string;
        agentRunning: string;
        checkedCreate: string;
        checkedRevise: string;
        more: string;
        abandonCreate: string;
        abandonRevise: string;
        conversationAria: string;
        workbenchAria: string;
        sessionUnavailable: string;
        retrying: string;
        retry: string;
        backToSkills: string;
        continueLater: string;
      };
      workbench: {
        packageAria: string;
        title: string;
        titleRevise: string;
        filesSurface: string;
        secretsSurface: string;
        secretsUnavailable: string;
        fileCount: (count: number) => string;
        diffSummary: (
          version: string,
          added: number,
          modified: number,
          deleted: number,
        ) => string;
        updating: string;
        readOnly: string;
        closeAria: string;
        empty: string;
        deletedFromBase: string;
        displayModeAria: string;
        source: string;
        preview: string;
        editFile: (path: string) => string;
        selectFile: string;
        baselineStale: string;
        unsavedHint: string;
        loadLatest: string;
        discard: string;
        saving: string;
        save: string;
        checkPassed: string;
        checkPassedWithWarnings: string;
        requiredSecrets: string;
        acknowledgeWarnings: string;
        checkSkill: string;
        commitCreate: string;
        commitRevise: string;
      };
      activity: {
        title: string;
        terminal: {
          completed: string;
          failed: string;
          stopped: string;
        };
        duration: (milliseconds: number) => string;
        attempt: (attempt: number) => string;
        reasoning: (attempt: number) => string;
        resultCount: (count: number) => string;
        sizeBytes: (count: number) => string;
        validationStages: {
          package_files: string;
          safety_scan: string;
        };
        stop: string;
        stopping: string;
        stages: Record<
          | "request_accepted"
          | "attempt_started"
          | "reasoning"
          | "tool_started"
          | "tool_completed"
          | "tool_failed"
          | "candidate_generated"
          | "validation_started"
          | "validation_passed"
          | "validation_failed"
          | "repair_started"
          | "run_terminal"
          | "commit_accepted"
          | "commit_validation_started"
          | "commit_validation_passed"
          | "commit_persistence_started"
          | "commit_persistence_completed"
          | "commit_terminal",
          string
        >;
        run: {
          pending: string;
          running: string;
          success: string;
          error: string;
          timeout: string;
          interrupted: string;
          cancelled: string;
        };
        tool: {
          pending: string;
          running: string;
          completed: string;
          failed: string;
        };
        toolSteps: (count: number) => string;
        noToolSteps: string;
        outputLimit: string;
      };
      composer: {
        mode: {
          flash: string;
          thinking: string;
          pro: string;
          ultra: string;
        };
        modeDescription: {
          flash: string;
          thinking: string;
          pro: string;
          ultra: string;
        };
        removeAttachment: (name: string) => string;
        addReference: string;
        selectModel: string;
        defaultModel: string;
        defaultBadge: string;
        selectThinking: string;
      };
      files: {
        tooltip: string;
        aria: string;
        label: string;
      };
      dialogs: {
        commitTitleCreate: string;
        commitTitleRevise: string;
        commitDescriptionCreate: (project: string) => string;
        commitDescriptionRevise: (slug: string, version: string) => string;
        backToReview: string;
        creating: string;
        creatingVersion: string;
        confirmCreate: string;
        confirmCreateVersion: string;
        staleTitle: string;
        staleDescription: (version: string) => string;
        confirmOverwrite: string;
        abandonTitleCreate: string;
        abandonTitleRevise: string;
        abandonDescriptionCreate: string;
        abandonDescriptionRevise: string;
        continueCreate: string;
        continueRevise: string;
        abandoning: string;
        confirmAbandon: string;
        discardTitle: string;
        discardDescription: string;
        continueEditing: string;
        discardAndLeave: string;
      };
      success: {
        created: string;
        withVersion: (version: number) => string;
        withoutVersion: string;
        goActivate: string;
        viewSkill: string;
        viewCandidateVersion: string;
        revisionWithSecrets: (version: number | null, count: number) => string;
        createdWithSecrets: (count: number) => string;
        configureSecrets: string;
      };
      versionConflict: {
        staleTitle: string;
        staleNamed: (live: number, base: number) => string;
        staleGeneric: string;
      };
    };
  };

  // Breadcrumb
  breadcrumb: {
    workspace: string;
    chats: string;
  };

  // Workspace
  workspace: {
    officialWebsite: string;
    settingsAndMore: string;
    contactUs: string;
    about: string;
    logout: string;
    gatewayUnavailable: string;
    gatewayUnavailableRetrying: string;
  };

  // Account-wide project workspace
  projectWorkspace: {
    title: string;
    subtitle: string;
    account: string;
    platformAdministration: string;
    systemSettings: string;
    privacyCenter: string;
    logout: string;
    searchProjects: string;
    searchPlaceholder: string;
    filterProjects: string;
    allProjects: string;
    pinnedOnly: string;
    projectCount: (count: number) => string;
    createProject: string;
    projectList: string;
    projectLoadFailed: string;
    loadingProjects: string;
    retry: string;
    columns: {
      project: string;
      description: string;
      actions: string;
    };
    card: {
      edit: string;
      editAction: string;
      pin: string;
      pinAction: string;
      unpin: string;
      pinned: string;
      noDescription: string;
      open: string;
    };
    empty: {
      noMatchesTitle: string;
      firstProjectTitle: string;
      noMatchesDescription: string;
      firstProjectDescription: string;
      clearFilters: string;
    };
    createDialog: {
      title: string;
      description: string;
      projectName: string;
      projectSlug: string;
      descriptionLabel: string;
      slugHelp: string;
      slugRequired: string;
      slugTooShort: string;
      slugTooLong: string;
      slugInvalid: string;
      cancel: string;
      creating: string;
      create: string;
    };
    editDialog: {
      title: string;
      slugImmutable: string;
      projectName: string;
      descriptionLabel: string;
      saving: string;
      save: string;
    };
    recovery: {
      title: string;
      windowEnd: string;
      recoverableUntil: (deadline: string) => string;
      recover: string;
      confirmTitle: string;
      confirmDescription: (projectName: string) => string;
      cancel: string;
      restoring: string;
      confirm: string;
      empty: string;
    };
    notifications: {
      trigger: string;
      unreadTrigger: (count: number) => string;
      title: string;
      description: string;
      loading: string;
      empty: string;
      retry: string;
      loadingMore: string;
      loadMore: string;
      readSyncPending: string;
      operationFailed: string;
      invitationTitle: string;
      invitedBy: (actor: string, projectName: string) => string;
      role: (role: string) => string;
      accepting: string;
      accept: string;
      joined: string;
      statuses: {
        pending: string;
        redeemed: string;
        revoked: string;
        expired: string;
      };
      roles: {
        editor: string;
        runner: string;
        viewer: string;
      };
    };
    errors: {
      slugConflict: string;
      unavailable: string;
      lastAdmin: string;
      memberQuotaExceeded: string;
      membershipVersionConflict: string;
      quotaStateConflict: string;
      invitationConflict: string;
      invitationInvalid: string;
      deletionStateConflict: string;
      validationFailed: string;
      authRequired: string;
      serviceUnavailable: string;
      requestFailed: string;
    };
  };

  // Conversation
  conversation: {
    noMessages: string;
    startConversation: string;
    branchCreated: string;
    branchFailed: string;
    runFailedTitle: string;
    runFailedDescription: string;
    providerUnavailableTitle: string;
    providerUnavailableDescription: string;
    modelQuotaExceededTitle: string;
    modelQuotaExceededDescription: string;
    modelAuthenticationFailedTitle: string;
    modelAuthenticationFailedDescription: string;
    modelProviderBusyTitle: string;
    modelProviderBusyDescription: string;
    modelCircuitOpenTitle: string;
    modelCircuitOpenDescription: string;
    modelRequestFailedTitle: string;
    modelRequestFailedDescription: string;
    runAdmissionNotConfirmedDescription: string;
    restoreFailedInput: string;
    restoreFailedInputBlocked: string;
    modelOutputLimitTitle: string;
    modelOutputLimitDescription: string;
    modelOutputLimitRetry: string;
    modelOutputLimitRetrying: string;
    loopSafetyLimitTitle: string;
    loopSafetyLimitDescription: string;
    loopFinalizationFailedTitle: string;
    loopFinalizationFailedDescription: string;
    toolExecutionFailedTitle: string;
    toolExecutionFailedDescription: string;
    runPolicyStaleTitle: string;
    runPolicyStaleDescription: string;
    toolCallControlStateInvalidTitle: string;
    toolCallControlStateInvalidDescription: string;
    toolCallControl: {
      progressLabel: string;
      repeatedWarningTitle: string;
      repeatedWarningDescription: (count: number, hardLimit: number) => string;
      repeatedLimitTitle: string;
      repeatedLimitDescription: string;
      toolBudgetWarningTitle: (toolName: string) => string;
      toolBudgetWarningDescription: (
        count: number,
        hardLimit: number,
      ) => string;
      toolBudgetExhaustedTitle: string;
      toolBudgetExhaustedDescription: string;
      subagentTotalLimitTitle: string;
      subagentTotalLimitDescription: string;
    };
    tokenBudgetReachedTitle: string;
    tokenBudgetReachedDescription: string;
    outputDeliveryIncompleteTitle: string;
    outputDeliveryIncompleteDescription: string;
    currentUploadUnavailableTitle: string;
    currentUploadUnavailableDescription: string;
    agentSuspendedTitle: string;
    agentSuspendedDescription: string;
    agentArchivedTitle: string;
    agentArchivedDescription: string;
    agentArchivedAction: string;
    agentModelUnavailableTitle: string;
    agentModelUnavailableDescription: string;
    runExecutionProfile: (
      modelDisplayName: string,
      modeName: string,
      supportsVision: boolean,
    ) => string;
    runWorkloadProfile: (profile: "interactive" | "research") => string;
  };

  // Chats
  chats: {
    searchChats: string;
    loadMoreToSearch: string;
    loadingMore: string;
    loadOlderChats: string;
  };

  // Sidecar
  sidecar: {
    title: string;
    open: string;
    close: string;
    delete: string;
    deleteConfirm: string;
    deleteSuccess: string;
    deleteFailed: string;
    addToConversation: string;
    askInSideChat: string;
    reference: string;
    selectedTextFragment: string;
    selectedTextFragments: string;
    clearReferences: string;
    emptyTitle: string;
    emptyDescription: string;
    placeholder: string;
    send: string;
    sendFailed: string;
    noContext: string;
    continuing: string;
    selectionCrossesMessages: string;
  };

  // Channels
  channels: {
    title: string;
    connect: string;
    modify: string;
    reconnect: string;
    disconnect: string;
    connected: string;
    notConnected: string;
    pending: string;
    revoked: string;
    disabled: string;
    unconfigured: string;
    unavailable: string;
    unavailableShort: string;
    setupTitle: (name: string) => string;
    setupEditTitle: (name: string) => string;
    setupDescription: string;
    saveAndConnect: string;
    saveChanges: string;
    descriptions: Record<string, string>;
    connectedAs: (name: string) => string;
  };

  // Page titles (document title)
  pages: {
    appName: string;
    chats: string;
    newChat: string;
    untitled: string;
  };

  // Tool calls
  toolCalls: {
    moreSteps: (count: number) => string;
    lessSteps: string;
    executionDetails: string;
    stepCount: (count: number) => string;
    executeCommand: string;
    presentFiles: string;
    needYourHelp: string;
    useTool: (toolName: string) => string;
    searchForRelatedInfo: string;
    searchForRelatedImages: string;
    searchFor: (query: string) => string;
    searchForRelatedImagesFor: (query: string) => string;
    searchOnWebFor: (query: string) => string;
    viewWebPage: string;
    listFolder: string;
    readFile: string;
    writeFile: string;
    clickToViewContent: string;
    writeTodos: string;
    rememberMemory: string;
    remembered: string;
    memoryDisabledNotSaved: string;
    skillInstallTooltip: string;
  };

  humanInput: {
    answered: string;
    pending: string;
    readOnly: string;
    attentionCount: (count: number) => string;
    changeBeforeSubmit: string;
    otherLabel: string;
    otherPlaceholder: string;
    submit: string;
    emptyError: string;
    requiredError: string;
    requiredA11yLabel: string;
    selectPlaceholder: string;
    availableOptions: string;
    requestedInformation: string;
    selected: string;
    yourAnswer: string;
    answeredValue: (value: string) => string;
  };

  executionApproval: {
    title: string;
    localHost: string;
    riskTitle: string;
    riskWarning: string;
    command: string;
    workingDirectory: string;
    sourceAgent: string;
    effectiveUser: string;
    timeout: string;
    timeoutSeconds: (seconds: number) => string;
    expiresIn: (seconds: number) => string;
    allowOnce: string;
    deny: string;
    allowing: string;
    denying: string;
    decisionFailed: string;
    exitCode: (code: number) => string;
    reason: string;
    finishedWarning: string;
    unknownTitle: string;
    unknownWarning: string;
    statuses: Record<
      | "pending"
      | "approved"
      | "claimed"
      | "finished"
      | "launch_failed"
      | "unknown"
      | "denied"
      | "expired"
      | "cancelled",
      string
    >;
  };

  // Uploads
  uploads: {
    uploading: string;
    uploadingFiles: string;
    ready: string;
    limitsHint: (
      maxFiles: number,
      maxFileSize: string,
      maxTotalSize: string,
    ) => string;
    filesTooLarge: (files: string, maxFileSize: string) => string;
    tooManyFiles: (count: number, maxFiles: number) => string;
    totalSizeTooLarge: (count: number, maxTotalSize: string) => string;
    projectStorageTooSmall: (count: number, remainingSize: string) => string;
    serverTooLarge: string;
    storageQuotaExceeded: string;
    preflightRejected: string;
    uploadFailed: string;
    cleanupFailed: string;
  };

  // Subtasks
  subtasks: {
    subtask: string;
    executing: (count: number) => string;
    in_progress: string;
    completed: string;
    failed: string;
    stopReasons: {
      token_capped: string;
      turn_capped: string;
      loop_capped: string;
      tool_budget_capped: string;
    };
  };

  // Token Usage
  tokenUsage: {
    title: string;
    label: string;
    input: string;
    output: string;
    total: string;
    view: string;
    unavailable: string;
    unavailableShort: string;
    note: string;
    presets: {
      off: string;
      summary: string;
      perTurn: string;
      debug: string;
    };
    presetDescriptions: {
      off: string;
      summary: string;
      perTurn: string;
      debug: string;
    };
    finalAnswer: string;
    stepTotal: string;
    sharedAttribution: string;
    subagent: (description: string) => string;
    startTodo: (content: string) => string;
    completeTodo: (content: string) => string;
    updateTodo: (content: string) => string;
    removeTodo: (content: string) => string;
  };

  // Context Window
  contextWindow: {
    title: string;
    usage: string;
    loading: string;
    unavailable: string;
    disabled: string;
    progressLabel: (percent: string) => string;
    usageWithoutCapacity: (estimated: string) => string;
    capacityUnavailable: string;
    contextWindowLimit: string;
    notConfigured: string;
    safetyBound: string;
    previousProviderInput: string;
    compressionConditions: string;
    noCompressionConditions: string;
    current: string;
    triggerAt: string;
    remaining: string;
    triggerStatus: string;
    triggerReached: string;
    estimatedContext: string;
    tokenThreshold: string;
    summaryPresent: string;
    allConditions: string;
    anyCondition: string;
    primary: string;
    triggerTypes: {
      tokens: string;
      fraction: string;
      messages: string;
    };
    tokens: (value: string) => string;
    messages: (count: number) => string;
  };

  // Shortcuts
  shortcuts: {
    searchActions: string;
    noResults: string;
    actions: string;
    keyboardShortcuts: string;
    keyboardShortcutsDescription: string;
    openCommandPalette: string;
    toggleSidebar: string;
  };

  // Settings
  settings: {
    title: string;
    description: string;
    sections: {
      account: string;
      personalization: string;
      appearance: string;
      channels: string;
      memory: string;
      tools: string;
      skills: string;
    };
    memory: {
      title: string;
    };
    personalization: {
      title: string;
      description: string;
      loading: string;
      loadError: string;
      loadErrorDescription: string;
      retry: string;
      enableTitle: string;
      enableDescription: string;
      platformUnavailable: string;
      saving: string;
      enableSuccess: string;
      disableSuccess: string;
      updateError: string;
      conflict: string;
      resetTitle: string;
      resetDescription: string;
      resetButton: string;
      resetDialogTitle: string;
      resetDialogDescription: string;
      resetChatNotice: string;
      cancel: string;
      confirmReset: string;
      resetting: string;
      resetSuccess: string;
      resetError: string;
    };
    appearance: {
      themeTitle: string;
      themeDescription: string;
      system: string;
      light: string;
      dark: string;
      systemDescription: string;
      lightDescription: string;
      darkDescription: string;
      chatWidthTitle: string;
      chatWidthDescription: string;
      chatWidthNarrow: string;
      chatWidthStandard: string;
      chatWidthWide: string;
      chatWidthFull: string;
      languageTitle: string;
      languageDescription: string;
    };
    tools: {
      title: string;
      description: string;
      adminRequired: string;
      empty: string;
    };
    channels: {
      title: string;
      description: string;
      disabled: string;
    };
    skills: {
      title: string;
      description: string;
      createSkill: string;
      emptyTitle: string;
      emptyDescription: string;
      emptyButton: string;
      adminRequired: string;
      installAdminRequired: string;
      viewSkill: (name: string) => string;
      toggleSkill: (name: string) => string;
      fileLabel: string;
      renderedDescription: string;
      enabled: string;
      disabled: string;
      categories: { public: string; custom: string; legacy: string };
      adminRequiredPreview: string;
      contentUnavailable: string;
      loadError: string;
      emptyContent: string;
      licenseLabel: string;
      loading: string;
      retry: string;
    };
    account: {
      profileTitle: string;
      email: string;
      username: string;
      role: string;
      roles: {
        system_admin: string;
        user: string;
      };
      changePasswordTitle: string;
      changePasswordDescription: string;
      ssoProvider: string;
      ssoPasswordDescription: string;
      ssoPasswordMessage: string;
      currentPassword: string;
      newPassword: string;
      confirmNewPassword: string;
      passwordMismatch: string;
      passwordTooShort: string;
      passwordChangedSuccess: string;
      networkError: string;
      updating: string;
      updatePassword: string;
      signOut: string;
    };
    acknowledge: {
      emptyTitle: string;
      emptyDescription: string;
    };
  };

  // Login / Auth
  login: {
    signInTitle: string;
    createAccountTitle: string;
    identifier: string;
    identifierPlaceholder: string;
    username: string;
    usernamePlaceholder: string;
    usernameHint: string;
    usernameInvalid: string;
    usernameTaken: string;
    email: string;
    emailPlaceholder: string;
    emailTaken: string;
    password: string;
    passwordPlaceholder: string;
    rememberMe: string;
    checkingRegistration: string;
    registrationUnavailable: string;
    registrationDisabled: string;
    retry: string;
    pleaseWait: string;
    signIn: string;
    createAccount: string;
    createAdminAccount: string;
    adminSetupRequiredTitle: string;
    adminSetupRequiredDescription: string;
    orContinueWith: string;
    ssoHint: string;
    continueWith: (provider: string) => string;
    noAccountSignUp: string;
    haveAccountSignIn: string;
    backToHome: string;
    networkError: string;
    authFailed: string;
    errors: {
      sso_failed: string;
      sso_cancelled: string;
      sso_account_exists: string;
      sso_not_allowed: string;
    };
  };

  // Administrator setup
  setup: {
    loading: string;
    initAdminTitle: string;
    initAdminDescription: string;
    username: string;
    usernamePlaceholder: string;
    usernameHint: string;
    usernameInvalid: string;
    email: string;
    emailPlaceholder: string;
    password: string;
    passwordPlaceholder: string;
    confirmPassword: string;
    confirmPasswordPlaceholder: string;
    passwordMismatch: string;
    passwordTooShort: string;
    networkError: string;
    creatingAccount: string;
    createAdminAccount: string;
    completeAdminTitle: string;
    completeAdminDescription: string;
    yourEmailPlaceholder: string;
    currentPassword: string;
    newPassword: string;
    confirmNewPassword: string;
    settingUp: string;
    completeSetup: string;
  };
}
