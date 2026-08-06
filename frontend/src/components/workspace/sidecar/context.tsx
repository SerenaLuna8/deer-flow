"use client";

import type { Message } from "@langchain/langgraph-sdk";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

import { usePrivateWorkAccess } from "@/core/private-work/provider";
import {
  adoptSidecarThread as guardSidecarAdoption,
  advanceSidecarIdentity,
  appendSidecarReference,
  buildMessageSidecarContext,
  createSidecarIdentity,
  getNextSidecarOpenState,
  guardSidecarClear,
  isCurrentSidecarIdentity,
  visibleSidecarThreadId,
  type SidecarContext,
  type SidecarIdentity,
  type SidecarReferenceStateItem,
  type SidecarThreadBinding,
} from "@/core/sidecar";
import { findLatestSidecarThread } from "@/core/sidecar/api";
import type { ThreadStreamOptions } from "@/core/threads/hooks";

export type SidecarReference = SidecarReferenceStateItem;

type SidecarContextValue = {
  open: boolean;
  activeReferences: SidecarReference[];
  conversationQuotes: SidecarReference[];
  parentThreadId: string;
  context: ThreadStreamOptions["context"];
  setContext: (context: ThreadStreamOptions["context"]) => void;
  identity: SidecarIdentity;
  captureIdentity: () => SidecarIdentity;
  isIdentityCurrent: (identity: SidecarIdentity) => boolean;
  sidecarThreadId: string | null;
  adoptSidecarThread: (identity: SidecarIdentity, threadId: string) => boolean;
  resetSidecar: (identity: SidecarIdentity) => boolean;
  restoreSidecarThread: (options?: {
    force?: boolean;
    identity?: SidecarIdentity;
  }) => Promise<string | null>;
  addContextToConversation: (context: SidecarContext) => void;
  clearConversationQuotes: (ids?: number[]) => void;
  clearActiveReferences: () => void;
  openSidecar: (identity: SidecarIdentity) => void;
  openContext: (context: SidecarContext) => void;
  openSelectedText: (
    message: Message,
    selectedText: string,
    displayIndex?: number,
  ) => void;
  close: () => void;
};

const SidecarContextObject = createContext<SidecarContextValue | null>(null);

export function SidecarProvider({
  children,
  parentThreadId,
  context,
}: {
  children: ReactNode;
  parentThreadId: string;
  context: ThreadStreamOptions["context"];
}) {
  const privateWork = usePrivateWorkAccess();
  const [open, setOpen] = useState(false);
  const [activeReferences, setActiveReferences] = useState<SidecarReference[]>(
    [],
  );
  const identityRef = useRef(createSidecarIdentity(parentThreadId));
  if (identityRef.current.parentThreadId !== parentThreadId) {
    identityRef.current = advanceSidecarIdentity(
      identityRef.current,
      parentThreadId,
    );
  }
  const identity = identityRef.current;
  const [, setIdentityRevision] = useState(identity.generation);
  const [sidecarThreadBinding, setSidecarThreadBinding] =
    useState<SidecarThreadBinding | null>(null);
  const [sidecarContext, setSidecarContext] =
    useState<ThreadStreamOptions["context"]>(context);
  const [conversationQuotes, setConversationQuotes] = useState<
    SidecarReference[]
  >([]);
  const referenceIdRef = useRef(0);
  const parentThreadIdRef = useRef(parentThreadId);
  const sidecarThreadBindingRef = useRef<SidecarThreadBinding | null>(null);
  const restoreRequestRef = useRef<{
    identity: SidecarIdentity;
    promise: Promise<string | null>;
  } | null>(null);

  const captureIdentity = useCallback(() => identityRef.current, []);
  const isIdentityCurrent = useCallback((candidate: SidecarIdentity) => {
    return isCurrentSidecarIdentity(identityRef.current, candidate);
  }, []);
  const updateSidecarThreadBinding = useCallback(
    (binding: SidecarThreadBinding | null) => {
      sidecarThreadBindingRef.current = binding;
      setSidecarThreadBinding(binding);
    },
    [],
  );
  const adoptSidecarThread = useCallback(
    (candidate: SidecarIdentity, threadId: string) => {
      const binding = guardSidecarAdoption(
        identityRef.current,
        candidate,
        threadId,
      );
      if (!binding) return false;
      updateSidecarThreadBinding(binding);
      return true;
    },
    [updateSidecarThreadBinding],
  );
  const sidecarThreadId = visibleSidecarThreadId(
    identity,
    sidecarThreadBinding,
  );

  const invalidateIdentity = useCallback(
    (candidate: SidecarIdentity, clearThread: boolean) => {
      const current = identityRef.current;
      if (!guardSidecarClear(current, candidate)) return false;
      const currentThreadId = visibleSidecarThreadId(
        current,
        sidecarThreadBindingRef.current,
      );
      const next = advanceSidecarIdentity(current);
      identityRef.current = next;
      setIdentityRevision(next.generation);
      updateSidecarThreadBinding(
        clearThread || !currentThreadId
          ? null
          : { identity: next, threadId: currentThreadId },
      );
      return true;
    },
    [updateSidecarThreadBinding],
  );

  const resetSidecar = useCallback(
    (candidate: SidecarIdentity) => {
      if (!invalidateIdentity(candidate, true)) return false;
      setOpen(false);
      setActiveReferences([]);
      return true;
    },
    [invalidateIdentity],
  );

  const createReference = useCallback((nextContext: SidecarContext) => {
    referenceIdRef.current += 1;
    return {
      id: referenceIdRef.current,
      context: nextContext,
    };
  }, []);

  useEffect(() => {
    if (parentThreadIdRef.current === parentThreadId) {
      return;
    }
    parentThreadIdRef.current = parentThreadId;
    setOpen(false);
    setActiveReferences([]);
    setSidecarContext(context);
    updateSidecarThreadBinding(null);
    setConversationQuotes([]);
  }, [context, parentThreadId, updateSidecarThreadBinding]);

  const restoreSidecarThread = useCallback(
    async (options?: { force?: boolean; identity?: SidecarIdentity }) => {
      const operationIdentity = options?.identity ?? captureIdentity();
      if (!isIdentityCurrent(operationIdentity)) return null;
      // A non-forced restore trusts the cached id; a forced restore always
      // re-queries the backend so a sidecar deleted elsewhere reconciles to
      // null instead of pointing the trigger at a dead thread (#3555).
      const currentThreadId = visibleSidecarThreadId(
        identityRef.current,
        sidecarThreadBindingRef.current,
      );
      if (!options?.force && currentThreadId) {
        return currentThreadId;
      }

      const restoreRequest = restoreRequestRef.current;
      if (
        restoreRequest &&
        isCurrentSidecarIdentity(restoreRequest.identity, operationIdentity)
      ) {
        return restoreRequest.promise;
      }

      const promise = findLatestSidecarThread({
        parentThreadId: operationIdentity.parentThreadId,
        apiClient: privateWork.client,
      })
        .then((thread) => {
          const threadId = thread?.thread_id ?? null;
          // Reconcile the cache with the backend: adopt a freshly found
          // thread, and on a forced refresh clear a stale id when the backend
          // no longer has a matching sidecar thread.
          if (threadId) {
            return adoptSidecarThread(operationIdentity, threadId)
              ? threadId
              : null;
          }
          if (options?.force) {
            resetSidecar(operationIdentity);
          }
          return null;
        })
        .catch(() => null)
        .finally(() => {
          if (restoreRequestRef.current?.promise === promise) {
            restoreRequestRef.current = null;
          }
        });

      restoreRequestRef.current = {
        identity: operationIdentity,
        promise,
      };

      return promise;
    },
    [
      adoptSidecarThread,
      captureIdentity,
      isIdentityCurrent,
      privateWork.client,
      resetSidecar,
    ],
  );

  useEffect(() => {
    void restoreSidecarThread({ identity: captureIdentity() });
  }, [captureIdentity, parentThreadId, restoreSidecarThread]);

  const openContext = useCallback(
    (nextContext: SidecarContext) => {
      const nextReference = createReference(nextContext);

      setActiveReferences(
        (references) =>
          getNextSidecarOpenState({
            open,
            sidecarThreadId,
            activeReferences: references,
            nextReference,
          }).activeReferences,
      );
      setOpen(true);
    },
    [createReference, open, sidecarThreadId],
  );

  const addContextToConversation = useCallback(
    (nextContext: SidecarContext) => {
      const nextReference = createReference(nextContext);
      setConversationQuotes((references) =>
        appendSidecarReference(references, nextReference),
      );
    },
    [createReference],
  );

  const clearConversationQuotes = useCallback((ids?: number[]) => {
    if (!ids) {
      setConversationQuotes([]);
      return;
    }
    const idsToClear = new Set(ids);
    setConversationQuotes((quotes) =>
      quotes.filter((quote) => !idsToClear.has(quote.id)),
    );
  }, []);

  const clearActiveReferences = useCallback(() => {
    setActiveReferences([]);
  }, []);

  const openSidecar = useCallback((candidate: SidecarIdentity) => {
    if (!isCurrentSidecarIdentity(identityRef.current, candidate)) return;
    setOpen(true);
  }, []);

  const openSelectedText = useCallback(
    (message: Message, selectedText: string, displayIndex?: number) => {
      const nextContext = buildMessageSidecarContext(message, displayIndex, {
        selectedText,
      });
      if (!nextContext) {
        return;
      }
      openContext(nextContext);
    },
    [openContext],
  );

  const close = useCallback(() => {
    invalidateIdentity(identityRef.current, false);
    setOpen(false);
  }, [invalidateIdentity]);

  const value = useMemo<SidecarContextValue>(
    () => ({
      open,
      activeReferences,
      conversationQuotes,
      parentThreadId,
      context: sidecarContext,
      setContext: setSidecarContext,
      identity,
      captureIdentity,
      isIdentityCurrent,
      sidecarThreadId,
      adoptSidecarThread,
      resetSidecar,
      restoreSidecarThread,
      addContextToConversation,
      clearConversationQuotes,
      clearActiveReferences,
      openSidecar,
      openContext,
      openSelectedText,
      close,
    }),
    [
      activeReferences,
      addContextToConversation,
      adoptSidecarThread,
      captureIdentity,
      clearActiveReferences,
      clearConversationQuotes,
      close,
      conversationQuotes,
      identity,
      isIdentityCurrent,
      open,
      openContext,
      openSelectedText,
      openSidecar,
      parentThreadId,
      restoreSidecarThread,
      resetSidecar,
      sidecarContext,
      sidecarThreadId,
    ],
  );

  return (
    <SidecarContextObject.Provider value={value}>
      {children}
    </SidecarContextObject.Provider>
  );
}

export function useMaybeSidecar() {
  return useContext(SidecarContextObject);
}

export function useSidecar() {
  const context = useMaybeSidecar();
  if (!context) {
    throw new Error("useSidecar must be used within a SidecarProvider");
  }
  return context;
}
