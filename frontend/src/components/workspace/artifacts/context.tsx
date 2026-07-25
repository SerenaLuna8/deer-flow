import {
  createContext,
  useCallback,
  useContext,
  useState,
  type ReactNode,
} from "react";

import { useSidebar } from "@/components/ui/sidebar";
import { isStaticWebsiteOnly } from "@/core/static-mode";

export interface ArtifactsContextType {
  enabled: boolean;
  artifacts: string[];
  setArtifacts: (artifacts: string[]) => void;

  selectedArtifact: string | null;
  autoSelect: boolean;
  select: (artifact: string, autoSelect?: boolean) => void;
  deselect: () => void;

  open: boolean;
  autoOpen: boolean;
  setOpen: (open: boolean) => void;
}

const ArtifactsContext = createContext<ArtifactsContextType | undefined>(
  undefined,
);

interface ArtifactsProviderProps {
  children: ReactNode;
  enabled?: boolean;
  onNavigationOpenChange?: (open: boolean) => void;
}

function ArtifactsStateProvider({
  children,
  enabled = true,
  setSidebarOpen,
}: ArtifactsProviderProps & {
  setSidebarOpen: (open: boolean) => void;
}) {
  const [artifacts, setArtifacts] = useState<string[]>([]);
  const [selectedArtifact, setSelectedArtifact] = useState<string | null>(null);
  const [autoSelect, setAutoSelect] = useState(true);
  const [open, setOpen] = useState(isStaticWebsiteOnly());
  const [autoOpen, setAutoOpen] = useState(true);

  const updateArtifacts = useCallback(
    (nextArtifacts: string[]) => {
      if (enabled) setArtifacts(nextArtifacts);
    },
    [enabled],
  );

  const select = useCallback(
    (artifact: string, autoSelect = false) => {
      if (!enabled) return;
      setSelectedArtifact(artifact);
      if (!isStaticWebsiteOnly()) {
        setSidebarOpen(false);
      }
      if (!autoSelect) {
        setAutoSelect(false);
      }
    },
    [enabled, setSidebarOpen, setSelectedArtifact, setAutoSelect],
  );

  const deselect = useCallback(() => {
    if (!enabled) return;
    setSelectedArtifact(null);
    setAutoSelect(true);
    setOpen(false);
  }, [enabled]);

  const value: ArtifactsContextType = {
    enabled,
    artifacts: enabled ? artifacts : [],
    setArtifacts: updateArtifacts,

    open: enabled && open,
    autoOpen: enabled && autoOpen,
    autoSelect: enabled && autoSelect,
    setOpen: (isOpen: boolean) => {
      if (!enabled) return;
      if (!isOpen && autoOpen) {
        setAutoOpen(false);
        setAutoSelect(false);
      }
      setOpen(isOpen);
    },

    selectedArtifact: enabled ? selectedArtifact : null,
    select,
    deselect,
  };

  return (
    <ArtifactsContext.Provider value={value}>
      {children}
    </ArtifactsContext.Provider>
  );
}

export function ArtifactsProvider({
  children,
  enabled = true,
}: ArtifactsProviderProps) {
  const { setOpen: setSidebarOpen } = useSidebar();
  return (
    <ArtifactsStateProvider enabled={enabled} setSidebarOpen={setSidebarOpen}>
      {children}
    </ArtifactsStateProvider>
  );
}

export function StandaloneArtifactsProvider({
  children,
  enabled = true,
  onNavigationOpenChange,
}: ArtifactsProviderProps) {
  return (
    <ArtifactsStateProvider
      enabled={enabled}
      setSidebarOpen={onNavigationOpenChange ?? (() => undefined)}
    >
      {children}
    </ArtifactsStateProvider>
  );
}

export function useArtifacts() {
  const context = useContext(ArtifactsContext);
  if (context === undefined) {
    throw new Error("useArtifacts must be used within an ArtifactsProvider");
  }
  return context;
}
