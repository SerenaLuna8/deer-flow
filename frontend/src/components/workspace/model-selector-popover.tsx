"use client";

import {
  createContext,
  useCallback,
  useContext,
  useState,
  type ComponentProps,
  type HTMLAttributes,
  type ReactNode,
} from "react";

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cn } from "@/lib/utils";

export type ModelSelectorProps = Omit<
  ComponentProps<typeof DropdownMenu>,
  "onOpenChange"
> & {
  onOpenChange?: (open: boolean) => void;
};

const ModelSelectorOpenContext = createContext<(open: boolean) => void>(
  () => undefined,
);

export const ModelSelector = ({
  defaultOpen = false,
  modal = false,
  onOpenChange,
  open,
  ...props
}: ModelSelectorProps) => {
  const [internalOpen, setInternalOpen] = useState(defaultOpen);
  const controlled = open !== undefined;
  const handleOpenChange = useCallback(
    (nextOpen: boolean) => {
      if (!controlled) {
        setInternalOpen(nextOpen);
      }
      onOpenChange?.(nextOpen);
    },
    [controlled, onOpenChange],
  );

  return (
    <ModelSelectorOpenContext.Provider value={handleOpenChange}>
      <DropdownMenu
        modal={modal}
        open={controlled ? open : internalOpen}
        onOpenChange={handleOpenChange}
        {...props}
      />
    </ModelSelectorOpenContext.Provider>
  );
};

export type ModelSelectorTriggerProps = ComponentProps<
  typeof DropdownMenuTrigger
>;

export const ModelSelectorTrigger = (props: ModelSelectorTriggerProps) => (
  <DropdownMenuTrigger {...props} />
);

export type ModelSelectorContentProps = ComponentProps<
  typeof DropdownMenuContent
> & {
  title?: ReactNode;
};

export const ModelSelectorContent = ({
  align = "end",
  side = "top",
  sideOffset = 8,
  collisionPadding = 12,
  className,
  children,
  onKeyDownCapture,
  title = "Model Selector",
  ...props
}: ModelSelectorContentProps) => {
  const setOpen = useContext(ModelSelectorOpenContext);

  return (
    <DropdownMenuContent
      aria-label={typeof title === "string" ? title : undefined}
      align={align}
      side={side}
      sideOffset={sideOffset}
      collisionPadding={collisionPadding}
      className={cn("w-70", className)}
      onKeyDownCapture={(event) => {
        onKeyDownCapture?.(event);
        if (!event.defaultPrevented && event.key === "Escape") {
          event.stopPropagation();
          setOpen(false);
        }
      }}
      {...props}
    >
      {children}
    </DropdownMenuContent>
  );
};

export type ModelSelectorLabelProps = ComponentProps<typeof DropdownMenuLabel>;

export const ModelSelectorLabel = ({
  className,
  ...props
}: ModelSelectorLabelProps) => (
  <DropdownMenuLabel
    className={cn("text-muted-foreground text-xs", className)}
    {...props}
  />
);

export type ModelSelectorListProps = HTMLAttributes<HTMLDivElement>;

export const ModelSelectorList = ({
  className,
  ...props
}: ModelSelectorListProps) => <div className={cn(className)} {...props} />;

export type ModelSelectorItemProps = ComponentProps<typeof DropdownMenuItem>;

export const ModelSelectorItem = ({
  className,
  ...props
}: ModelSelectorItemProps) => (
  <DropdownMenuItem className={cn("py-2.5", className)} {...props} />
);

export type ModelSelectorNameProps = ComponentProps<"span">;

export const ModelSelectorName = ({
  className,
  ...props
}: ModelSelectorNameProps) => (
  <span
    className={cn("flex-1 truncate text-left text-sm font-medium", className)}
    {...props}
  />
);
