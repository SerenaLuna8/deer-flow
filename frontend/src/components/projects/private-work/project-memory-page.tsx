"use client";

import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import { useDeferredValue, useEffect, useMemo, useState } from "react";

import { MemoryV2Workbench } from "@/components/projects/private-work/memory/memory-v2-workbench";
import { GatewayApiError } from "@/core/api/errors";
import {
  acceptProjectMemoryV2Candidate,
  disableProjectMemoryV2Fact,
  exportProjectMemoryV2,
  getProjectMemoryV2Fact,
  getProjectMemoryV2Status,
  hardForgetProjectMemoryV2Fact,
  listProjectMemoryV2Candidates,
  listProjectMemoryV2Facts,
  projectMemoryV2CandidatesQueryKey,
  projectMemoryV2FactDetailQueryKey,
  projectMemoryV2FactsQueryKey,
  projectMemoryV2MutationKey,
  projectMemoryV2Permissions,
  projectMemoryV2RootQueryKey,
  projectMemoryV2StatusQueryKey,
  rejectProjectMemoryV2Candidate,
  restoreProjectMemoryV2Fact,
  reviseProjectMemoryV2Fact,
  type MemoryV2Candidate,
  type MemoryV2Fact,
  type MemoryV2FactListStatus,
} from "@/core/private-work/memory";
import { usePrivateWorkAccess } from "@/core/private-work/provider";
import { runPrivateWorkAbortable } from "@/core/private-work/types";
import type { Project } from "@/core/projects/types";

const MEMORY_PAGE_SIZE = 25;
const MEMORY_FETCH_LIMIT = MEMORY_PAGE_SIZE + 1;

function optionalFilter(value: string) {
  const normalized = value.trim();
  return normalized.length > 0 ? normalized : undefined;
}

export function ProjectMemoryPage({ project }: { project: Project }) {
  const privateWork = usePrivateWorkAccess();
  const queryClient = useQueryClient();
  const scope = privateWork.scope;
  const permissions = projectMemoryV2Permissions(project.capabilities);
  const [factPage, setFactPage] = useState(0);
  const [candidatePage, setCandidatePage] = useState(0);
  const [factStatus, setFactStatus] =
    useState<MemoryV2FactListStatus>("active");
  const [factQuery, setFactQuery] = useState("");
  const [factCategory, setFactCategory] = useState("");
  const [selectedFactId, setSelectedFactId] = useState<string | null>(null);
  const deferredFactQuery = useDeferredValue(factQuery);
  const deferredFactCategory = useDeferredValue(factCategory);
  const factRequest = useMemo(
    () => ({
      status: factStatus,
      limit: MEMORY_FETCH_LIMIT,
      offset: factPage * MEMORY_PAGE_SIZE,
      query: optionalFilter(deferredFactQuery),
      category: optionalFilter(deferredFactCategory),
    }),
    [deferredFactCategory, deferredFactQuery, factPage, factStatus],
  );
  const candidateRequest = useMemo(
    () => ({
      limit: MEMORY_FETCH_LIMIT,
      offset: candidatePage * MEMORY_PAGE_SIZE,
    }),
    [candidatePage],
  );

  useEffect(() => {
    setFactPage(0);
  }, [deferredFactCategory, deferredFactQuery, factStatus]);

  const factsQuery = useQuery({
    queryKey: projectMemoryV2FactsQueryKey(scope, factRequest),
    queryFn: ({ signal }) =>
      listProjectMemoryV2Facts(privateWork, factRequest, signal),
    enabled: permissions.canRead,
    placeholderData: keepPreviousData,
  });
  const candidatesQuery = useQuery({
    queryKey: projectMemoryV2CandidatesQueryKey(scope, candidateRequest),
    queryFn: ({ signal }) =>
      listProjectMemoryV2Candidates(privateWork, candidateRequest, signal),
    enabled: permissions.canRead,
    placeholderData: keepPreviousData,
  });
  const statusQuery = useQuery({
    queryKey: projectMemoryV2StatusQueryKey(scope),
    queryFn: ({ signal }) => getProjectMemoryV2Status(privateWork, signal),
    enabled: permissions.canRead,
  });
  const detailQuery = useQuery({
    queryKey: projectMemoryV2FactDetailQueryKey(
      scope,
      selectedFactId ?? "none",
    ),
    queryFn: ({ signal }) =>
      getProjectMemoryV2Fact(privateWork, selectedFactId!, signal),
    enabled: permissions.canRead && selectedFactId !== null,
  });

  const memoryRootQueryKey = projectMemoryV2RootQueryKey(scope);
  const refreshMemory = () =>
    queryClient.invalidateQueries({ queryKey: memoryRootQueryKey });
  const refreshMemoryOnConflict = (error: Error) =>
    error instanceof GatewayApiError && error.status === 409
      ? refreshMemory()
      : undefined;

  const acceptCandidate = useMutation({
    mutationKey: projectMemoryV2MutationKey(scope, "accept-candidate"),
    mutationFn: (candidate: MemoryV2Candidate) =>
      runPrivateWorkAbortable(privateWork, (signal) =>
        acceptProjectMemoryV2Candidate(privateWork, candidate, signal),
      ),
    onSuccess: refreshMemory,
    onError: refreshMemoryOnConflict,
  });
  const rejectCandidate = useMutation({
    mutationKey: projectMemoryV2MutationKey(scope, "reject-candidate"),
    mutationFn: (candidate: MemoryV2Candidate) =>
      runPrivateWorkAbortable(privateWork, (signal) =>
        rejectProjectMemoryV2Candidate(privateWork, candidate, signal),
      ),
    onSuccess: refreshMemory,
    onError: refreshMemoryOnConflict,
  });
  const reviseFact = useMutation({
    mutationKey: projectMemoryV2MutationKey(scope, "revise-fact"),
    mutationFn: ({
      fact,
      input,
    }: {
      fact: MemoryV2Fact;
      input: {
        content?: string;
        category?: string;
        confidence?: number;
        reason?: string;
      };
    }) =>
      runPrivateWorkAbortable(privateWork, (signal) =>
        reviseProjectMemoryV2Fact(privateWork, fact, input, signal),
      ),
    onSuccess: refreshMemory,
    onError: refreshMemoryOnConflict,
  });
  const disableFact = useMutation({
    mutationKey: projectMemoryV2MutationKey(scope, "disable-fact"),
    mutationFn: (fact: MemoryV2Fact) =>
      runPrivateWorkAbortable(privateWork, (signal) =>
        disableProjectMemoryV2Fact(privateWork, fact, signal),
      ),
    onSuccess: refreshMemory,
    onError: refreshMemoryOnConflict,
  });
  const restoreFact = useMutation({
    mutationKey: projectMemoryV2MutationKey(scope, "restore-fact"),
    mutationFn: (fact: MemoryV2Fact) =>
      runPrivateWorkAbortable(privateWork, (signal) =>
        restoreProjectMemoryV2Fact(privateWork, fact, signal),
      ),
    onSuccess: refreshMemory,
    onError: refreshMemoryOnConflict,
  });
  const hardForgetFact = useMutation({
    mutationKey: projectMemoryV2MutationKey(scope, "hard-forget"),
    mutationFn: (fact: MemoryV2Fact) =>
      runPrivateWorkAbortable(privateWork, (signal) =>
        hardForgetProjectMemoryV2Fact(privateWork, fact, signal),
      ),
    onSuccess: (_result, fact) => {
      if (selectedFactId === fact.id) setSelectedFactId(null);
      return refreshMemory();
    },
    onError: refreshMemoryOnConflict,
  });
  const exportMemory = useMutation({
    mutationKey: projectMemoryV2MutationKey(scope, "export"),
    mutationFn: () =>
      runPrivateWorkAbortable(privateWork, (signal) =>
        exportProjectMemoryV2(privateWork, signal),
      ),
  });

  const factItems = factsQuery.data?.items.slice(0, MEMORY_PAGE_SIZE) ?? [];
  const candidateItems =
    candidatesQuery.data?.items.slice(0, MEMORY_PAGE_SIZE) ?? [];
  const busyCandidateIds = [
    acceptCandidate.isPending ? acceptCandidate.variables?.id : null,
    rejectCandidate.isPending ? rejectCandidate.variables?.id : null,
  ].filter((value): value is string => Boolean(value));
  const busyFactIds = [
    reviseFact.isPending ? reviseFact.variables?.fact.id : null,
    disableFact.isPending ? disableFact.variables?.id : null,
    restoreFact.isPending ? restoreFact.variables?.id : null,
    hardForgetFact.isPending ? hardForgetFact.variables?.id : null,
  ].filter((value): value is string => Boolean(value));

  async function downloadMemory() {
    const blob = await exportMemory.mutateAsync();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `deer-flow-memory-v2-${new Date().toISOString().replace(/[:.]/gu, "-")}.ndjson`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  }

  return (
    <MemoryV2Workbench
      projectName={project.display_name}
      projectSlug={project.slug}
      facts={{
        items: factItems,
        page: factPage,
        hasNext: (factsQuery.data?.items.length ?? 0) > MEMORY_PAGE_SIZE,
        isLoading: factsQuery.isLoading,
        isFetching: factsQuery.isFetching,
        error: factsQuery.error,
        retry: () => void factsQuery.refetch(),
        previous: () => setFactPage((page) => Math.max(0, page - 1)),
        next: () => setFactPage((page) => page + 1),
        query: factQuery,
        category: factCategory,
        status: factStatus,
        setQuery: setFactQuery,
        setCategory: setFactCategory,
        setStatus: setFactStatus,
      }}
      candidates={{
        items: candidateItems,
        page: candidatePage,
        hasNext: (candidatesQuery.data?.items.length ?? 0) > MEMORY_PAGE_SIZE,
        isLoading: candidatesQuery.isLoading,
        isFetching: candidatesQuery.isFetching,
        error: candidatesQuery.error,
        retry: () => void candidatesQuery.refetch(),
        previous: () => setCandidatePage((page) => Math.max(0, page - 1)),
        next: () => setCandidatePage((page) => page + 1),
      }}
      detail={{
        selectedFactId,
        data: detailQuery.data ?? null,
        isLoading: detailQuery.isLoading,
        error: detailQuery.error,
        retry: () => void detailQuery.refetch(),
        select: setSelectedFactId,
      }}
      status={{
        data: statusQuery.data ?? null,
        isLoading: statusQuery.isLoading,
        error: statusQuery.error,
        retry: () => void statusQuery.refetch(),
      }}
      actions={{
        canManage: permissions.canManage,
        canHardForget: permissions.canHardForget,
        canExport: permissions.canExport,
        isExporting: exportMemory.isPending,
        busyCandidateIds,
        busyFactIds,
        exportMemory: downloadMemory,
        acceptCandidate: (candidate) =>
          acceptCandidate.mutateAsync(candidate).then(() => undefined),
        rejectCandidate: (candidate) =>
          rejectCandidate.mutateAsync(candidate).then(() => undefined),
        reviseFact: (fact, input) =>
          reviseFact.mutateAsync({ fact, input }).then(() => undefined),
        disableFact: (fact) =>
          disableFact.mutateAsync(fact).then(() => undefined),
        restoreFact: (fact) =>
          restoreFact.mutateAsync(fact).then(() => undefined),
        hardForgetFact: (fact) =>
          hardForgetFact.mutateAsync(fact).then(() => undefined),
      }}
    />
  );
}
