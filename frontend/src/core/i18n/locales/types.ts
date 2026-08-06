import type { LucideIcon } from "lucide-react";

export interface Translations {
  // Locale meta
  locale: {
    localName: string;
  };

  // Common
  common: {
    home: string;
    settings: string;
    delete: string;
    edit: string;
    rename: string;
    share: string;
    openInNewWindow: string;
    close: string;
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
    blog: string;
  };

  // Welcome
  welcome: {
    greeting: string;
    description: string;
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
    badge: (count: number, additions: number, deletions: number) => string;
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
    dreamQueued: string;
    dreamAlreadyRunning: string;
    dreamNothingPending: string;
    dreamInvalidArguments: string;
    dreamLogInvalidArguments: string;
    dreamRestoreInvalidArguments: string;
    dreamAttachmentsUnsupported: string;
    dreamFailed: string;
    dreamRequiresThread: string;
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
    usage: string;
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
        loading: string;
        unavailableTitle: string;
        unavailableDescription: string;
        thresholdReached: string;
        used: string;
        reserved: string;
        limit: string;
        tightenTitle: string;
        updateError: string;
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
      };
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
      page: (page: number) => string;
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
      description: string;
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
        checkedAt: string;
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
      description: string;
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
      description: string;
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
      };
      retrySafety: {
        safe: string;
        unsafe: string;
        unknown: string;
      };
      filters: {
        label: string;
        project: string;
        projectQuery: string;
        projectQueryPlaceholder: string;
        status: string;
        type: string;
        allStatuses: string;
        allTypes: string;
        apply: string;
        clear: string;
        invalidQuery: string;
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
      older: string;
    };
  };

  adminAssets: {
    navigation: {
      platformLabel: string;
      projectLabel: string;
      agent: string;
      skill: string;
      mcp: string;
      credential: string;
    };
    shell: {
      platformAria: string;
      systemCatalog: string;
      systemCatalogDescription: string;
      adminScope: string;
      projectAria: string;
      backToProjects: string;
      projectGovernance: string;
      projectBoundary: string;
      projectId: string;
    };
    common: {
      assetVersion: string;
      versionId: string;
      mcpConfigurationId: string;
      currentPublishedVersion: string;
      currentPublishedMcpConfiguration: string;
      updatedAt: string;
      versionHistory: string;
      mcpConfigurationHistory: string;
      versionCount: (count: number) => string;
      mcpConfigurationCount: (count: number) => string;
      credentialMetadata: string;
      details: string;
      dangerZone: string;
      type: string;
      credentialTypes: {
        modelApiKey: string;
        apiKey: string;
        token: string;
        mcpAuth: string;
        oauth: string;
        database: string;
      };
      transportTypes: {
        stdio: string;
        sse: string;
        http: string;
      };
      credentialPayloadGroups: {
        env: string;
        headers: string;
        query: string;
        oauth: string;
      };
      metadataVersion: string;
      replaceCredential: string;
      migrateReferences: string;
      revokeCredential: string;
      delete: string;
      createCredential: string;
      createProjectCredential: string;
      createProjectAsset: string;
      retry: string;
      retrying: string;
      create: string;
      creating: string;
      createVersion: string;
      creatingVersion: string;
      reload: string;
      systemProvided: string;
      projectOwned: string;
      active: string;
      revoked: string;
      loading: string;
      migrationSuccess: string;
      credentialRotationNote: string;
      historySchemaUnavailable: string;
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
    pages: {
      systemEyebrow: string;
      databaseCatalog: string;
      runtimeReadOnly: string;
      controlledWrite: string;
      loading: string;
      loadFailed: string;
      systemCount: (count: number) => string;
      credentialCount: (count: number) => string;
      systemNote: (kind: string) => string;
      emptySystem: (kind: string) => string;
      emptyCreate: string;
      system: {
        agentsTitle: string;
        agentsDescription: string;
        skillsTitle: string;
        skillsDescription: string;
        mcpTitle: string;
        mcpDescription: string;
        credentialsTitle: string;
        credentialsDescription: string;
      };
      projectEyebrow: string;
      projectDatabaseCatalog: string;
      sharedOnly: string;
      projectLoadFailed: string;
      sourceCounts: (systemCount: number, projectCount: number) => string;
      project: {
        agentsTitle: string;
        agentsDescription: string;
        skillsTitle: string;
        skillsDescription: string;
        mcpTitle: string;
        mcpDescription: string;
        credentialsTitle: string;
        credentialsDescription: string;
      };
    };
    catalog: {
      systemAssets: string;
      systemAssetsDescription: string;
      searchPlaceholder: string;
      filterAll: string;
      catalogReady: string;
      catalogReadyDetail: string;
      totalAssets: string;
      activeAssets: string;
      unpublishedAssets: string;
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
      credentialSource: string;
      systemCredentials: string;
      projectCredentials: string;
      emptyCredentials: (title: string) => string;
      waitingForAdmin: string;
      archive: string;
      activate: string;
      disable: string;
      suspend: string;
    };
    version: {
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
      configureGrants: string;
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
      credentialRequirements: string;
      transport: string;
      command: string;
      url: string;
      arguments: string;
      timeout: string;
      credentialSlots: string;
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
      projectHeadersOnly: string;
      missingSystemCommand: string;
      missingSystemUrl: string;
      systemEnvOnly: string;
      systemRemoteCredentialsOnly: string;
    };
    dialogs: {
      createAssetTitle: (kind: string) => string;
      skillCreationDescription: string;
      assetCreationDescription: (scope: "system" | "project") => string;
      addMcpTitle: string;
      addMcpDescription: string;
      addMcpSubmit: string;
      addAndPublish: string;
      addAndApprove: string;
      addAndSubmitApproval: string;
      retryMcpApproval: string;
      mcpSavedApprovalFailed: string;
      mcpSavedRetryApprovalOnly: string;
      addingMcp: string;
      editMcpConfigTitle: string;
      saveMcpConfig: string;
      saveAndPublishMcpConfig: string;
      saveAndApproveMcpConfig: string;
      saveAndSubmitMcpConfig: string;
      savingMcpConfig: string;
      name: string;
      assetSlug: string;
      slugTitle: string;
      slugHelp: string;
      filePath: string;
      mediaType: string;
      fileContent: string;
      skillTemplateDescription: string;
      skillTemplateInstructions: string;
      description: string;
      transport: string;
      sseTransport: string;
      httpTransport: string;
      mcpServiceUrl: string;
      urlQueryRemoved: string;
      authentication: string;
      headerAuthentication: string;
      queryAuthentication: string;
      noAuthentication: string;
      noAuthenticationHelp: string;
      connectionAndAuthentication: string;
      needsProjectCredential: string;
      slotName: string;
      slotNameTitle: string;
      slotNameHelp: string;
      purpose: string;
      credentialFieldGroup: string;
      requiredFields: string;
      requiredFieldsHelp: string;
      requestHeaderName: string;
      queryParameterName: string;
      credentialFieldNameTitle: string;
      credentialFieldNameHelp: string;
      queryGroup: string;
      unsupportedMcpTransport: string;
      missingMcpUrl: string;
      invalidMcpUrl: string;
      mcpUrlQuery: string;
      unsupportedMcpCredentialGroup: string;
      missingMcpCredentialSlotName: string;
      missingMcpCredentialFields: string;
      missingMcpHeaderName: string;
      missingMcpQueryName: string;
      invalidMcpCredentialFieldName: string;
      projectCredential: string;
      createProjectCredential: string;
      credentialSelectedByAdmin: string;
      noCompatibleCredential: string;
      compatibleCredentialsOnly: string;
      credentialFieldsMatch: string;
      adminCompletesApproval: string;
      safetyPreview: string;
      configurationPreviewReadonly: string;
      serviceAddress: string;
      waitingForServiceAddress: string;
      pendingCredentialSelection: string;
      encryptedRead: string;
      secretNeverDisplayed: string;
      credentialSource: string;
      encryptedProjectCredential: string;
      publicationStatus: string;
      publishOnSave: string;
      publishAfterApproval: string;
      publicationFlow: string;
      saveMcpStep: string;
      saveMcpStepDetail: string;
      selectCredentialStep: string;
      selectCredentialStepDetail: string;
      approvePublishStep: string;
      approvePublishStepDetail: string;
      approvalRunsAfterSave: string;
      createVersionTitle: (kind: string) => string;
      secretCreateTitle: string;
      secretReplaceTitle: string;
      secretDescription: string;
      credentialSlug: string;
      credentialFields: string;
      credentialFieldsHelp: string;
      fixedCredentialFieldsHelp: string;
      addField: string;
      group: string;
      envGroup: string;
      headersGroup: string;
      fieldName: string;
      credentialValue: string;
      removeField: (index: number) => string;
      remove: string;
      writing: string;
      encryptWrite: string;
      validation: {
        emptyFields: string;
        unsupportedGroup: string;
        emptyField: string;
        fieldTooLong: string;
        duplicateField: string;
        emptyValue: string;
      };
      revokeTitle: string;
      revokeDescription: (name: string) => string;
      cancel: string;
      revoking: string;
      confirmRevoke: string;
      migrateTitle: string;
      migrateDescription: (name: string) => string;
      migrating: string;
      confirmMigrate: string;
      deleteTitle: string;
      deleteDescription: (name: string) => string;
      deleting: string;
      confirmDeleteCountdown: (seconds: number) => string;
      confirmDelete: string;
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
      };
      approval: {
        configureTitle: string;
        configureDescription: string;
        saveGrants: string;
        configureEmptyOptional: string;
        clearOptionalGrant: string;
        publishTitle: string;
        publishDescription: string;
        approve: string;
        publishEmptyOptional: string;
        selectCredential: string;
        currentVersion: string;
        loadingCredentials: string;
        credentialsFailed: string;
        requiredUnavailable: string;
      };
    };
    rotation: {
      title: string;
      summary: (current: number, total: number) => string;
      current: string;
      pending: (count: number) => string;
    };
    errors: {
      notFound: string;
      forbidden: string;
      conflict: string;
      validationFailed: string;
      mcpVersionValidation: string;
      mcpCredentialMismatch: string;
      storageQuota: string;
      storageUnavailable: string;
      authRequired: string;
      network: string;
      invalidResponse: string;
      invalidErrorResponse: string;
      fallback: string;
    };
  };

  adminModelSettings: {
    header: {
      eyebrow: string;
      title: string;
      description: string;
      create: string;
    };
    overview: {
      label: string;
      configured: string;
      configuredDetail: string;
      active: string;
      activeDetail: string;
      defaultModel: string;
      defaultDetail: string;
      notSet: string;
      revision: string;
      revisionDetail: string;
    };
    states: {
      loading: string;
      unavailableTitle: string;
      unavailableDescription: string;
      retry: string;
      emptyTitle: string;
      emptyDescription: string;
      catalogLabel: string;
      catalogDescription: string;
      modelCount: (count: number) => string;
    };
    card: {
      defaultModel: string;
      active: string;
      suspended: string;
      updatedAt: (formattedDate: string) => string;
      updatedAtColumn: string;
      providerModel: string;
      credential: string;
      environmentKey: string;
      status: string;
      version: string;
      versionMeta: (
        versionNumber: number,
        revision: number,
        sortOrder: number,
      ) => string;
      capabilities: string;
      noCapabilities: string;
      thinking: string;
      reasoningEffort: string;
      vision: string;
      edit: string;
      pause: string;
      enable: string;
      currentDefault: string;
      setDefault: string;
      actions: string;
      actionFor: (action: string, name: string) => string;
      defaultCannotPause: string;
      credentialUnbound: string;
      credentialUnavailable: string;
      credentialHistorical: string;
    };
    adapters: {
      patchedOpenAI: string;
      patchedDeepSeek: string;
      patchedMiMo: string;
      patchedMiniMax: string;
      patchedStepFun: string;
    };
    editor: {
      editTitle: string;
      createTitle: string;
      description: string;
      basicInformation: string;
      basicDescription: string;
      logicalName: string;
      displayName: string;
      displayNamePlaceholder: string;
      providerAdapter: string;
      providerModel: string;
      status: string;
      active: string;
      suspended: string;
      modelDescription: string;
      modelDescriptionPlaceholder: string;
      sortOrder: string;
      sortOrderHint: string;
      capabilities: string;
      capabilitiesAndRuntime: string;
      capabilitiesDescription: string;
      supportsThinking: string;
      supportsReasoningEffort: string;
      supportsVision: string;
      commonProviderSettings: string;
      commonProviderSettingsDescription: string;
      baseUrl: string;
      baseUrlHint: string;
      temperature: string;
      maxTokens: string;
      requestTimeout: string;
      maxRetries: string;
      credentialBinding: string;
      credentialBindingDescription: string;
      systemCredential: string;
      credentialsUnavailableHint: string;
      credentialSelectionHint: string;
      selectCredential: string;
      providerDoesNotUseCredential: string;
      environmentKey: string;
      environmentKeyHint: string;
      testConnection: string;
      testingConnection: string;
      testConnectionDescription: string;
      connectionSucceeded: string;
      connectionFailed: string;
      advancedJson: string;
      advancedJsonHint: string;
      cancel: string;
      saving: string;
      saveChanges: string;
      createModel: string;
    };
    validation: {
      invalidNumber: (label: string) => string;
      temperature: string;
      maxTokens: string;
      requestTimeout: string;
      maxRetries: string;
      sortOrder: string;
      advancedJsonInvalid: string;
      advancedJsonObject: string;
      advancedJsonUnsafe: string;
      invalidForm: string;
      invalidConfiguration: string;
    };
    actionErrors: {
      authRequired: string;
      conflict: string;
      invalid: string;
      generic: string;
    };
    success: {
      updated: (name: string) => string;
      created: (name: string) => string;
      enabled: (name: string) => string;
      suspended: (name: string) => string;
      defaultSet: (name: string) => string;
    };
  };

  adminSystemSettings: {
    header: {
      eyebrow: string;
      title: string;
      description: string;
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
      quotas: { title: string; description: string };
      agentRuntime: { title: string; description: string };
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
    create: string;
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
    title: string;
    description: string;
    newAgent: string;
    emptyTitle: string;
    emptyDescription: string;
    featureDisabledTitle: string;
    featureDisabledDescription: string;
    chat: string;
    delete: string;
    deleteConfirm: string;
    deleteSuccess: string;
    newChat: string;
    createPageTitle: string;
    createPageSubtitle: string;
    nameStepTitle: string;
    nameStepHint: string;
    nameStepPlaceholder: string;
    nameStepContinue: string;
    nameStepInvalidError: string;
    nameStepAlreadyExistsError: string;
    nameStepNetworkError: string;
    nameStepCheckError: string;
    nameStepCheckErrorWithDetail: string;
    nameStepApiDisabledError: string;
    nameStepBootstrapMessage: string;
    save: string;
    saving: string;
    saveRequested: string;
    saveHint: string;
    agentCreatedPendingRefresh: string;
    more: string;
    agentCreated: string;
    startChatting: string;
    backToGallery: string;
  };

  // Breadcrumb
  breadcrumb: {
    workspace: string;
    chats: string;
  };

  // Workspace
  workspace: {
    officialWebsite: string;
    githubTooltip: string;
    settingsAndMore: string;
    visitGithub: string;
    reportIssue: string;
    contactUs: string;
    about: string;
    logout: string;
    gatewayUnavailable: string;
    gatewayUnavailableRetrying: string;
  };

  // Conversation
  conversation: {
    noMessages: string;
    startConversation: string;
    branchCreated: string;
    branchFailed: string;
    runFailedTitle: string;
    runFailedDescription: string;
    agentModelUnavailableTitle: string;
    agentModelUnavailableDescription: string;
    runExecutionProfile: (
      modelName: string,
      modeName: string,
      supportsVision: boolean,
    ) => string;
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

  // Uploads
  uploads: {
    uploading: string;
    uploadingFiles: string;
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
  };

  // Subtasks
  subtasks: {
    subtask: string;
    executing: (count: number) => string;
    in_progress: string;
    completed: string;
    failed: string;
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
    automaticCompression: string;
    loading: string;
    unavailable: string;
    disabled: string;
    progressLabel: (percent: string) => string;
    current: string;
    triggerAt: string;
    remaining: string;
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
    tokenPair: (current: string, total: string) => string;
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
      notification: string;
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
    notification: {
      title: string;
      description: string;
      requestPermission: string;
      deniedHint: string;
      testButton: string;
      testTitle: string;
      testBody: string;
      notSupported: string;
      disableNotification: string;
    };
    account: {
      profileTitle: string;
      email: string;
      role: string;
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
    email: string;
    emailPlaceholder: string;
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
