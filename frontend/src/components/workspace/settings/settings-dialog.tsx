"use client";

import { PaletteIcon, SparklesIcon, UserIcon } from "lucide-react";
import { useEffect, useState } from "react";

import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { ScrollArea } from "@/components/ui/scroll-area";
import { AccountSettingsPage } from "@/components/workspace/settings/account-settings-page";
import { AppearanceSettingsPage } from "@/components/workspace/settings/appearance-settings-page";
import { PersonalizationSettingsPage } from "@/components/workspace/settings/personalization-settings-page";
import {
  SETTINGS_SECTION_IDS,
  type SettingsSectionId,
} from "@/components/workspace/settings/settings-sections";
import { useI18n } from "@/core/i18n/hooks";
import { cn } from "@/lib/utils";

const settingsSectionIcons = {
  account: UserIcon,
  personalization: SparklesIcon,
  appearance: PaletteIcon,
} as const;

type SettingsDialogProps = React.ComponentProps<typeof Dialog> & {
  defaultSection?: SettingsSectionId;
};

function settingsNavigationItemClassName(active: boolean): string {
  return cn(
    "relative flex w-full items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors before:pointer-events-none before:absolute before:inset-y-2 before:left-0 before:w-0.5 before:rounded-full",
    active
      ? "bg-blue-50 text-blue-600 before:bg-blue-600 dark:bg-blue-500/15 dark:text-blue-300 dark:before:bg-blue-300"
      : "text-muted-foreground hover:bg-blue-50 before:bg-transparent dark:hover:bg-blue-500/15",
  );
}

export function SettingsDialog(props: SettingsDialogProps) {
  const { defaultSection = "appearance", ...dialogProps } = props;
  const { t } = useI18n();
  const [activeSection, setActiveSection] =
    useState<SettingsSectionId>(defaultSection);

  useEffect(() => {
    // When opening the dialog, ensure the active section follows the caller's intent.
    if (dialogProps.open) {
      setActiveSection(defaultSection);
    }
  }, [defaultSection, dialogProps.open]);

  const sections = SETTINGS_SECTION_IDS.map((id) => ({
    id,
    label: t.settings.sections[id],
    icon: settingsSectionIcons[id],
  }));
  return (
    <Dialog
      {...dialogProps}
      onOpenChange={(open) => props.onOpenChange?.(open)}
    >
      <DialogContent
        className="flex h-[75vh] max-h-[calc(100vh-2rem)] flex-col sm:max-w-5xl md:max-w-6xl"
        aria-describedby={undefined}
      >
        <DialogHeader className="gap-1">
          <DialogTitle>{t.settings.title}</DialogTitle>
          <p className="text-muted-foreground text-sm">
            {t.settings.description}
          </p>
        </DialogHeader>
        <div className="grid min-h-0 flex-1 gap-4 md:grid-cols-[220px_minmax(0,1fr)]">
          <nav className="bg-sidebar min-h-0 overflow-y-auto rounded-lg border p-2">
            <ul className="space-y-1 pr-1">
              {sections.map(({ id, label, icon: Icon }) => {
                const active = activeSection === id;
                return (
                  <li key={id}>
                    <button
                      type="button"
                      aria-current={active ? "page" : undefined}
                      onClick={() => setActiveSection(id)}
                      className={settingsNavigationItemClassName(active)}
                    >
                      <Icon className="size-4" />
                      <span>{label}</span>
                    </button>
                  </li>
                );
              })}
            </ul>
          </nav>
          <ScrollArea className="bg-background h-full min-h-0 rounded-lg border">
            <div className="space-y-8 p-6">
              {activeSection === "account" && <AccountSettingsPage />}
              {activeSection === "personalization" && (
                <PersonalizationSettingsPage />
              )}
              {activeSection === "appearance" && <AppearanceSettingsPage />}
            </div>
          </ScrollArea>
        </div>
      </DialogContent>
    </Dialog>
  );
}
