import { FilesIcon, XIcon } from "lucide-react";
import { usePathname } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";

import { ConversationEmptyState } from "@/components/ai-elements/conversation";
import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { useI18n } from "@/core/i18n/hooks";
import { usePrivateWorkAccess } from "@/core/private-work/provider";
import { isStaticWebsiteOnly } from "@/core/static-mode";
import { useUploadedFiles } from "@/core/uploads";
import { useIsMobile } from "@/hooks/use-mobile";
import { cn } from "@/lib/utils";

import {
  ArtifactFileDetail,
  ArtifactFileList,
  useArtifacts,
} from "../artifacts";
import { useThread } from "../messages/context";
import { SidecarPanel, useMaybeSidecar } from "../sidecar";

const RIGHT_PANEL_ANIMATION_MS = 280;

export type ChatRightPanelKind = "sidecar" | "artifacts";

export function resolveChatRightPanel({
  sidecarOpen,
  artifactsEnabled,
  artifactsOpen,
  hasArtifacts,
  staticWebsiteOnly,
}: {
  sidecarOpen: boolean;
  artifactsEnabled: boolean;
  artifactsOpen: boolean;
  hasArtifacts: boolean;
  staticWebsiteOnly: boolean;
}): ChatRightPanelKind | null {
  if (sidecarOpen) return "sidecar";
  if (!artifactsEnabled || !artifactsOpen) return null;
  if (staticWebsiteOnly && !hasArtifacts) return null;
  return "artifacts";
}

const ChatBox: React.FC<{ children: React.ReactNode; threadId: string }> = ({
  children,
  threadId,
}) => {
  const { locale, t } = useI18n();
  const { thread } = useThread();
  const isMobile = useIsMobile();
  const pathname = usePathname();
  const threadIdRef = useRef(threadId);
  const privateWork = usePrivateWorkAccess();

  const {
    enabled: artifactsEnabled,
    artifacts,
    open: artifactsOpen,
    setOpen: setArtifactsOpen,
    setArtifacts,
    select: selectArtifact,
    deselect,
    selectedArtifact,
  } = useArtifacts();
  const projectFiles = useUploadedFiles(
    threadId,
    privateWork,
    artifactsEnabled,
  );
  const sidecar = useMaybeSidecar();
  const sidecarOpen = sidecar?.open ?? false;

  const [autoSelectFirstArtifact, setAutoSelectFirstArtifact] = useState(true);
  const threadArtifacts = useMemo(() => {
    const stateArtifacts = Array.isArray(thread.values.artifacts)
      ? thread.values.artifacts
      : [];
    return Array.from(
      new Set([
        ...stateArtifacts,
        ...(projectFiles.data?.files.flatMap((file) =>
          file.logical_path ? [file.logical_path] : [],
        ) ?? []),
      ]),
    );
  }, [projectFiles.data?.files, thread.values.artifacts]);

  useEffect(() => {
    if (threadIdRef.current !== threadId) {
      threadIdRef.current = threadId;
      deselect();
      setArtifacts([]);
    }

    // Update artifacts from the current thread
    setArtifacts(threadArtifacts);

    // DO NOT automatically deselect the artifact when switching threads, because the artifacts auto discovering is not work now.
    // if (
    //   selectedArtifact &&
    //   !thread.values.artifacts?.includes(selectedArtifact)
    // ) {
    //   deselect();
    // }

    if (isStaticWebsiteOnly() && autoSelectFirstArtifact) {
      if (threadArtifacts.length > 0) {
        setAutoSelectFirstArtifact(false);
        selectArtifact(threadArtifacts[0]!);
      }
    }
  }, [
    threadId,
    autoSelectFirstArtifact,
    deselect,
    selectArtifact,
    selectedArtifact,
    setArtifacts,
    threadArtifacts,
  ]);

  const activeRightPanel = useMemo(
    () =>
      resolveChatRightPanel({
        sidecarOpen,
        artifactsEnabled,
        artifactsOpen,
        hasArtifacts: artifacts.length > 0,
        staticWebsiteOnly: isStaticWebsiteOnly(),
      }),
    [artifacts.length, artifactsEnabled, artifactsOpen, sidecarOpen],
  );
  const rightPanelOpen = activeRightPanel !== null;
  const [renderedRightPanel, setRenderedRightPanel] =
    useState<ChatRightPanelKind | null>(activeRightPanel);

  const resizableIdBase = useMemo(() => {
    return pathname.replace(/[^a-zA-Z0-9_-]+/g, "-").replace(/^-+|-+$/g, "");
  }, [pathname]);

  useEffect(() => {
    if (activeRightPanel) {
      setRenderedRightPanel(activeRightPanel);
      return;
    }

    const timeout = window.setTimeout(() => {
      setRenderedRightPanel(null);
    }, RIGHT_PANEL_ANIMATION_MS);

    return () => {
      window.clearTimeout(timeout);
    };
  }, [activeRightPanel]);

  useEffect(() => {
    if (sidecarOpen && artifactsOpen) {
      setArtifactsOpen(false);
    }
  }, [artifactsOpen, setArtifactsOpen, sidecarOpen]);

  const rightPanelContent = useMemo(() => {
    if (renderedRightPanel === "sidecar") {
      return <SidecarPanel />;
    }
    if (
      artifactsEnabled &&
      renderedRightPanel === "artifacts" &&
      selectedArtifact
    ) {
      return (
        <ArtifactFileDetail
          className="size-full"
          filepath={selectedArtifact}
          threadId={threadId}
        />
      );
    }
    if (artifactsEnabled && renderedRightPanel === "artifacts") {
      return (
        <div className="relative flex size-full justify-center">
          <div className="absolute top-1 right-1 z-30">
            <Button
              size="icon-sm"
              variant="ghost"
              onClick={() => {
                setArtifactsOpen(false);
              }}
            >
              <XIcon />
            </Button>
          </div>
          {artifacts.length === 0 ? (
            <ConversationEmptyState
              icon={<FilesIcon />}
              title={
                locale === "zh-CN" ? "尚未选择文件" : "No artifact selected"
              }
              description={
                locale === "zh-CN"
                  ? "选择一个文件以查看详情"
                  : "Select an artifact to view its details"
              }
            />
          ) : (
            <div className="flex size-full max-w-(--container-width-sm) flex-col justify-center p-4 pt-8">
              <header className="shrink-0">
                <h2 className="text-lg font-medium">{t.common.artifacts}</h2>
              </header>
              <main className="min-h-0 grow">
                <ArtifactFileList
                  className="max-w-(--container-width-sm) p-4 pt-12"
                  files={artifacts}
                  threadId={threadId}
                />
              </main>
            </div>
          )}
        </div>
      );
    }
    return null;
  }, [
    renderedRightPanel,
    artifactsEnabled,
    selectedArtifact,
    threadId,
    artifacts,
    setArtifactsOpen,
    locale,
    t.common.artifacts,
  ]);

  if (!artifactsEnabled && sidecar == null) {
    return <div className="relative size-full min-w-0">{children}</div>;
  }

  if (isMobile) {
    return (
      <>
        <div className="relative size-full min-w-0">{children}</div>
        <Sheet
          open={rightPanelOpen}
          onOpenChange={(open) => {
            if (open) {
              return;
            }
            if (sidecarOpen) {
              sidecar?.close();
            }
            if (artifactsOpen) {
              setArtifactsOpen(false);
            }
          }}
        >
          <SheetContent
            className="w-[calc(100vw-1rem)] max-w-none gap-0 p-0 sm:max-w-md [&>button]:hidden"
            side="right"
          >
            <SheetHeader className="sr-only">
              <SheetTitle>
                {renderedRightPanel === "sidecar"
                  ? t.sidecar.title
                  : t.common.artifacts}
              </SheetTitle>
              <SheetDescription>
                {locale === "zh-CN"
                  ? "查看当前对话的侧边内容。"
                  : "Browse the side panel for this conversation."}
              </SheetDescription>
            </SheetHeader>
            <div className="min-h-0 flex-1 p-3 pt-10">{rightPanelContent}</div>
          </SheetContent>
        </Sheet>
      </>
    );
  }

  return (
    <div
      id={`${resizableIdBase}-panels`}
      className={cn(
        "[container-type:inline-size] grid size-full min-h-0 transition-[grid-template-columns] duration-[280ms] ease-out motion-reduce:transition-none",
        rightPanelOpen
          ? "grid-cols-[minmax(0,1fr)_1px_minmax(0,40%)]"
          : "grid-cols-[minmax(0,1fr)_0px_0px]",
      )}
    >
      <div className="relative min-h-0 min-w-0" id="chat">
        {children}
      </div>
      <div
        id={`${resizableIdBase}-separator`}
        aria-hidden="true"
        className={cn(
          "bg-border opacity-33 transition-opacity duration-200 ease-out motion-reduce:transition-none",
          !rightPanelOpen && "pointer-events-none opacity-0",
        )}
      />
      <aside
        aria-hidden={!rightPanelOpen}
        className={cn(
          "min-h-0 min-w-0 overflow-hidden transition-opacity duration-[280ms] ease-out motion-reduce:transition-none",
          !rightPanelOpen && "pointer-events-none opacity-0",
        )}
        id="artifacts"
      >
        <div
          className={cn(
            "ml-auto h-full w-[40cqw] transition-opacity duration-[280ms] ease-out motion-reduce:transition-none",
            renderedRightPanel === "sidecar" ? "p-0" : "p-4",
            rightPanelOpen ? "opacity-100" : "opacity-0",
          )}
        >
          {rightPanelContent}
        </div>
      </aside>
    </div>
  );
};

export { ChatBox };
