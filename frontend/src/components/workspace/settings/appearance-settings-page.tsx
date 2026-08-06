"use client";

import { MonitorSmartphoneIcon, MoonIcon, SunIcon } from "lucide-react";
import { useTheme } from "next-themes";
import { useMemo, type ComponentType, type SVGProps } from "react";

import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Separator } from "@/components/ui/separator";
import { enUS, isLocale, zhCN, type Locale } from "@/core/i18n";
import { useI18n } from "@/core/i18n/hooks";
import {
  CHAT_CONTENT_WIDTH_OPTIONS,
  useLocalSettings,
  type ChatContentWidth,
} from "@/core/settings";
import { cn } from "@/lib/utils";

import { SettingsSection } from "./settings-section";

const languageOptions: { value: Locale; label: string }[] = [
  { value: "en-US", label: enUS.locale.localName },
  { value: "zh-CN", label: zhCN.locale.localName },
];

export function AppearanceSettingsPage() {
  const { t, locale, changeLocale } = useI18n();
  const { theme, setTheme, systemTheme } = useTheme();
  const [localSettings, setLocalSettings] = useLocalSettings();
  const currentTheme = (theme ?? "system") as "system" | "light" | "dark";

  const themeOptions = useMemo(
    () => [
      {
        id: "system",
        label: t.settings.appearance.system,
        description: t.settings.appearance.systemDescription,
        icon: MonitorSmartphoneIcon,
      },
      {
        id: "light",
        label: t.settings.appearance.light,
        description: t.settings.appearance.lightDescription,
        icon: SunIcon,
      },
      {
        id: "dark",
        label: t.settings.appearance.dark,
        description: t.settings.appearance.darkDescription,
        icon: MoonIcon,
      },
    ],
    [
      t.settings.appearance.dark,
      t.settings.appearance.darkDescription,
      t.settings.appearance.light,
      t.settings.appearance.lightDescription,
      t.settings.appearance.system,
      t.settings.appearance.systemDescription,
    ],
  );
  const chatWidthLabels: Record<ChatContentWidth, string> = {
    narrow: t.settings.appearance.chatWidthNarrow,
    standard: t.settings.appearance.chatWidthStandard,
    wide: t.settings.appearance.chatWidthWide,
    full: t.settings.appearance.chatWidthFull,
  };
  const chatWidthPreviewWidths: Record<ChatContentWidth, string> = {
    narrow: "48%",
    standard: "64%",
    wide: "82%",
    full: "100%",
  };

  return (
    <div className="space-y-8">
      <SettingsSection
        title={t.settings.appearance.themeTitle}
        description={t.settings.appearance.themeDescription}
      >
        <div className="grid gap-3 lg:grid-cols-3">
          {themeOptions.map((option) => (
            <ThemePreviewCard
              key={option.id}
              icon={option.icon}
              label={option.label}
              description={option.description}
              active={currentTheme === option.id}
              mode={option.id as "system" | "light" | "dark"}
              systemTheme={systemTheme}
              onSelect={(value) => setTheme(value)}
            />
          ))}
        </div>
      </SettingsSection>

      <Separator />

      <SettingsSection
        title={t.settings.appearance.chatWidthTitle}
        description={t.settings.appearance.chatWidthDescription}
      >
        <div
          className="grid grid-cols-2 gap-3 lg:grid-cols-4"
          data-testid="chat-content-width-options"
        >
          {CHAT_CONTENT_WIDTH_OPTIONS.map((option) => (
            <ChatWidthPreviewCard
              key={option}
              label={chatWidthLabels[option]}
              previewWidth={chatWidthPreviewWidths[option]}
              active={localSettings.appearance.chatContentWidth === option}
              ariaLabel={`${t.settings.appearance.chatWidthTitle}: ${chatWidthLabels[option]}`}
              onSelect={() =>
                setLocalSettings("appearance", {
                  chatContentWidth: option,
                })
              }
            />
          ))}
        </div>
      </SettingsSection>

      <Separator />

      <SettingsSection
        title={t.settings.appearance.languageTitle}
        description={t.settings.appearance.languageDescription}
      >
        <Select
          value={locale}
          onValueChange={(value) => {
            if (isLocale(value)) {
              changeLocale(value);
            }
          }}
        >
          <SelectTrigger className="w-[220px]">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {languageOptions.map((item) => (
              <SelectItem key={item.value} value={item.value}>
                {item.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </SettingsSection>
    </div>
  );
}

function ChatWidthPreviewCard({
  label,
  previewWidth,
  active,
  ariaLabel,
  onSelect,
}: {
  label: string;
  previewWidth: string;
  active: boolean;
  ariaLabel: string;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      aria-label={ariaLabel}
      aria-pressed={active}
      onClick={onSelect}
      className={cn(
        "group rounded-lg border p-3 text-left transition-all",
        active
          ? "border-selection bg-selection-subtle/35 ring-selection/20 shadow-sm ring-2"
          : "hover:border-selection/35 hover:bg-muted/20 hover:shadow-sm",
      )}
    >
      <div className="bg-muted/35 flex h-16 items-stretch justify-center rounded-md border p-2">
        <div
          className="bg-background flex min-w-5 flex-col gap-1.5 rounded border px-2 py-2 shadow-xs transition-[width]"
          style={{ width: previewWidth }}
        >
          <span className="bg-foreground/15 h-1.5 w-4/5 rounded-full" />
          <span className="bg-foreground/10 h-1.5 w-3/5 rounded-full" />
          <span className="bg-selection/25 mt-auto h-2 rounded-full" />
        </div>
      </div>
      <div className="mt-2 text-center text-sm font-medium">{label}</div>
    </button>
  );
}

function ThemePreviewCard({
  icon: Icon,
  label,
  description,
  active,
  mode,
  systemTheme,
  onSelect,
}: {
  icon: ComponentType<SVGProps<SVGSVGElement>>;
  label: string;
  description: string;
  active: boolean;
  mode: "system" | "light" | "dark";
  systemTheme?: string;
  onSelect: (mode: "system" | "light" | "dark") => void;
}) {
  const previewMode =
    mode === "system" ? (systemTheme === "dark" ? "dark" : "light") : mode;
  return (
    <button
      type="button"
      onClick={() => onSelect(mode)}
      className={cn(
        "group flex h-full flex-col gap-3 rounded-lg border p-4 text-left transition-all",
        active
          ? "border-selection bg-selection-subtle/35 ring-selection/20 shadow-sm ring-2"
          : "hover:border-selection/35 hover:bg-muted/20 hover:shadow-sm",
      )}
    >
      <div className="flex items-start gap-3">
        <div
          className={cn(
            "rounded-md p-2",
            active
              ? "bg-selection-subtle text-foreground"
              : "bg-muted text-foreground",
          )}
        >
          <Icon className="size-4" />
        </div>
        <div className="space-y-1">
          <div className="text-sm leading-none font-semibold">{label}</div>
          <p className="text-muted-foreground text-xs leading-snug">
            {description}
          </p>
        </div>
      </div>
      <div
        className={cn(
          "relative overflow-hidden rounded-md border text-xs transition-colors",
          previewMode === "dark"
            ? "border-neutral-800 bg-neutral-900 text-neutral-200"
            : "border-slate-200 bg-white text-slate-900",
        )}
      >
        <div className="border-border/50 flex items-center gap-2 border-b px-3 py-2">
          <div
            className={cn(
              "h-2 w-2 rounded-full",
              previewMode === "dark" ? "bg-emerald-400" : "bg-success",
            )}
          />
          <div className="h-2 w-10 rounded-full bg-current/20" />
          <div className="h-2 w-6 rounded-full bg-current/15" />
        </div>
        <div className="grid grid-cols-[1fr_240px] gap-3 px-3 py-3">
          <div className="space-y-2">
            <div className="h-3 w-3/4 rounded-full bg-current/15" />
            <div className="h-3 w-1/2 rounded-full bg-current/10" />
            <div className="h-[90px] rounded-md border border-current/10 bg-current/5" />
          </div>
          <div className="space-y-2">
            <div className="flex items-center gap-2">
              <div className="h-8 w-8 rounded-md bg-current/10" />
              <div className="space-y-2">
                <div className="h-2 w-14 rounded-full bg-current/15" />
                <div className="h-2 w-10 rounded-full bg-current/10" />
              </div>
            </div>
            <div className="flex flex-col gap-1 rounded-md border border-dashed border-current/15 p-2">
              <div className="h-2 w-3/5 rounded-full bg-current/15" />
              <div className="h-2 w-2/5 rounded-full bg-current/10" />
            </div>
          </div>
        </div>
      </div>
    </button>
  );
}
