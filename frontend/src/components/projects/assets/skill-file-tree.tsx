"use client";

import {
  ChevronRightIcon,
  FileCode2Icon,
  FolderIcon,
  FolderOpenIcon,
} from "lucide-react";
import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

import type {
  SkillFileTreeNode,
  SkillFileTreeSelection,
} from "./skill-file-workbench-state";

const FILE_STATE_LABEL = {
  unchanged: null,
  modified: "已修改",
  added: "新增",
} as const;

export const SKILL_FILE_TREE_PAGE_SIZE = 100;

export function skillFileTreePageWindow<T>(
  items: readonly T[],
  requestedPage: number,
): {
  items: readonly T[];
  page: number;
  pageCount: number;
} {
  const pageCount = Math.max(
    1,
    Math.ceil(items.length / SKILL_FILE_TREE_PAGE_SIZE),
  );
  const page = Math.min(Math.max(0, Math.trunc(requestedPage)), pageCount - 1);
  const start = page * SKILL_FILE_TREE_PAGE_SIZE;
  return {
    items: items.slice(start, start + SKILL_FILE_TREE_PAGE_SIZE),
    page,
    pageCount,
  };
}

function pageForSelection(
  nodes: readonly SkillFileTreeNode[],
  selection: SkillFileTreeSelection | null,
): number | null {
  if (!selection) return null;
  const index = nodes.findIndex(
    (node) =>
      node.path === selection.path ||
      (node.kind === "folder" && selection.path.startsWith(`${node.path}/`)),
  );
  return index < 0 ? null : Math.floor(index / SKILL_FILE_TREE_PAGE_SIZE);
}

function TreeNodes({
  nodes,
  selection,
  expandedFolders,
  depth,
  parentPath,
  onSelectFile,
  onSelectFolder,
  onToggleFolder,
}: {
  nodes: readonly SkillFileTreeNode[];
  selection: SkillFileTreeSelection | null;
  expandedFolders: ReadonlySet<string>;
  depth: number;
  parentPath: string;
  onSelectFile: (path: string) => void;
  onSelectFolder: (path: string) => void;
  onToggleFolder: (path: string) => void;
}) {
  const [requestedPage, setRequestedPage] = useState(
    () => pageForSelection(nodes, selection) ?? 0,
  );
  const selectedPage = pageForSelection(nodes, selection);
  useEffect(() => {
    if (selectedPage === null) return;
    setRequestedPage(selectedPage);
  }, [selectedPage]);
  const pageWindow = skillFileTreePageWindow(nodes, requestedPage);

  return (
    <>
      {pageWindow.items.map((node) => {
        const selected =
          selection?.kind === node.kind && selection.path === node.path;
        const paddingLeft = 8 + depth * 16;

        if (node.kind === "file") {
          return (
            <li key={node.path} role="none">
              <button
                type="button"
                role="treeitem"
                aria-label={`文件 ${node.path}`}
                aria-selected={selected}
                className={cn(
                  "hover:bg-background focus-visible:ring-ring flex w-full items-center gap-2 rounded-lg py-2 pr-2 text-left text-sm transition-colors focus-visible:ring-2 focus-visible:outline-none",
                  selected && "bg-background shadow-sm",
                )}
                style={{ paddingLeft }}
                onClick={() => onSelectFile(node.path)}
              >
                <FileCode2Icon
                  aria-hidden
                  className="text-muted-foreground size-4 shrink-0"
                />
                <span className="min-w-0 flex-1 truncate font-medium">
                  {node.name}
                </span>
                {node.file.state !== "unchanged" && (
                  <span className="text-primary shrink-0 text-[10px] font-medium">
                    {FILE_STATE_LABEL[node.file.state]}
                  </span>
                )}
              </button>
            </li>
          );
        }

        const expanded = expandedFolders.has(node.path);
        return (
          <li key={node.path} role="none">
            <button
              type="button"
              role="treeitem"
              aria-label={`文件夹 ${node.path}`}
              aria-selected={selected}
              aria-expanded={expanded}
              className={cn(
                "hover:bg-background focus-visible:ring-ring flex w-full items-center gap-1.5 rounded-lg py-2 pr-2 text-left text-sm transition-colors focus-visible:ring-2 focus-visible:outline-none",
                selected && "bg-background shadow-sm",
              )}
              style={{ paddingLeft }}
              onClick={() => {
                onSelectFolder(node.path);
                onToggleFolder(node.path);
              }}
            >
              <ChevronRightIcon
                aria-hidden
                className={cn(
                  "text-muted-foreground size-3.5 shrink-0 transition-transform",
                  expanded && "rotate-90",
                )}
              />
              {expanded ? (
                <FolderOpenIcon
                  aria-hidden
                  className="text-muted-foreground size-4 shrink-0"
                />
              ) : (
                <FolderIcon
                  aria-hidden
                  className="text-muted-foreground size-4 shrink-0"
                />
              )}
              <span className="min-w-0 flex-1 truncate font-medium">
                {node.name}
              </span>
            </button>
            {expanded && node.children.length > 0 && (
              <ul role="group">
                <TreeNodes
                  nodes={node.children}
                  selection={selection}
                  expandedFolders={expandedFolders}
                  depth={depth + 1}
                  parentPath={node.path}
                  onSelectFile={onSelectFile}
                  onSelectFolder={onSelectFolder}
                  onToggleFolder={onToggleFolder}
                />
              </ul>
            )}
          </li>
        );
      })}
      {pageWindow.pageCount > 1 ? (
        <li role="none">
          <div
            role="group"
            aria-label={`${parentPath || "根目录"}文件分页`}
            className="flex items-center justify-between gap-1 py-1 pr-1"
            style={{ paddingLeft: 8 + depth * 16 }}
          >
            <Button
              type="button"
              size="sm"
              variant="ghost"
              className="h-7 px-2 text-xs"
              disabled={pageWindow.page === 0}
              onClick={() => setRequestedPage(pageWindow.page - 1)}
            >
              上一页
            </Button>
            <span
              role="status"
              className="text-muted-foreground text-[10px] whitespace-nowrap tabular-nums"
            >
              第 {pageWindow.page + 1} / {pageWindow.pageCount} 页
            </span>
            <Button
              type="button"
              size="sm"
              variant="ghost"
              className="h-7 px-2 text-xs"
              disabled={pageWindow.page + 1 >= pageWindow.pageCount}
              onClick={() => setRequestedPage(pageWindow.page + 1)}
            >
              下一页
            </Button>
          </div>
        </li>
      ) : null}
    </>
  );
}

export function SkillFileTree({
  nodes,
  selection,
  expandedFolders,
  onSelectFile,
  onSelectFolder,
  onToggleFolder,
}: {
  nodes: readonly SkillFileTreeNode[];
  selection: SkillFileTreeSelection | null;
  expandedFolders: ReadonlySet<string>;
  onSelectFile: (path: string) => void;
  onSelectFolder: (path: string) => void;
  onToggleFolder: (path: string) => void;
}) {
  return (
    <nav aria-label="Skill 文件">
      <ul role="tree" aria-label="Skill 文件目录">
        <TreeNodes
          nodes={nodes}
          selection={selection}
          expandedFolders={expandedFolders}
          depth={0}
          parentPath=""
          onSelectFile={onSelectFile}
          onSelectFolder={onSelectFolder}
          onToggleFolder={onToggleFolder}
        />
      </ul>
    </nav>
  );
}
