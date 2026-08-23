import {
  adminModelSettingsSchemaForProvider,
  type AdminModelProviderAdapterDescriptor,
  type AdminModelProviderSettingField,
  type AdminModelSettingValue,
} from "./types";

export type AdminModelProviderSettingsDraft = {
  provider_adapter: string;
  values: Readonly<Record<string, string>>;
  original_keys: readonly string[];
  preserved_settings: Readonly<Record<string, AdminModelSettingValue>>;
  incompatible_keys: readonly string[];
  unknown_provider: boolean;
};

export class AdminModelProviderSettingsDraftError extends Error {
  constructor(message = "Provider settings are incompatible") {
    super(message);
    this.name = "AdminModelProviderSettingsDraftError";
  }
}

function hasOwn(
  settings: Readonly<Record<string, AdminModelSettingValue>>,
  key: string,
): boolean {
  return Object.hasOwn(settings, key);
}

function defaultDraftValue(field: AdminModelProviderSettingField): string {
  if (field.default_mode === "provider") return "";
  const value = field.default_value;
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "number" || typeof value === "string") {
    return String(value);
  }
  throw new AdminModelProviderSettingsDraftError(
    `Provider setting ${field.name} has no scalar platform default`,
  );
}

function storedDraftValue(
  field: AdminModelProviderSettingField,
  value: AdminModelSettingValue,
): string {
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "number" || typeof value === "string") {
    return String(value);
  }
  throw new AdminModelProviderSettingsDraftError(
    `Provider setting ${field.name} is not scalar`,
  );
}

function isPreservedField(field: AdminModelProviderSettingField): boolean {
  return field.form_control === "preserve" || field.input_type === "json";
}

export function createAdminModelProviderSettingsDraft(
  descriptor: AdminModelProviderAdapterDescriptor | null | undefined,
  storedSettings: Readonly<Record<string, AdminModelSettingValue>>,
  providerAdapter = descriptor?.id ?? "",
): AdminModelProviderSettingsDraft {
  if (!descriptor) {
    return {
      provider_adapter: providerAdapter,
      values: {},
      original_keys: Object.keys(storedSettings),
      preserved_settings: {},
      incompatible_keys: Object.keys(storedSettings).sort(),
      unknown_provider: true,
    };
  }

  const fields = new Map(
    descriptor.setting_fields.map((field) => [field.name, field] as const),
  );
  const values: Record<string, string> = {};
  const preservedSettings: Record<string, AdminModelSettingValue> = {};
  const incompatibleKeys = Object.keys(storedSettings).filter(
    (key) => !fields.has(key),
  );
  const providerSchema = adminModelSettingsSchemaForProvider(descriptor);

  for (const field of descriptor.setting_fields) {
    const saved = hasOwn(storedSettings, field.name);
    if (isPreservedField(field)) {
      if (saved) {
        const value = storedSettings[field.name];
        if (
          value !== undefined &&
          providerSchema.safeParse({ [field.name]: value }).success
        ) {
          preservedSettings[field.name] = value;
        } else {
          incompatibleKeys.push(field.name);
        }
      }
      continue;
    }
    if (!saved) {
      values[field.name] = defaultDraftValue(field);
      continue;
    }
    const value = storedSettings[field.name];
    if (
      value === undefined ||
      !providerSchema.safeParse({ [field.name]: value }).success
    ) {
      incompatibleKeys.push(field.name);
      continue;
    }
    try {
      values[field.name] = storedDraftValue(field, value);
    } catch {
      incompatibleKeys.push(field.name);
    }
  }

  return {
    provider_adapter: descriptor.id,
    values,
    original_keys: Object.keys(storedSettings),
    preserved_settings: preservedSettings,
    incompatible_keys: [...new Set(incompatibleKeys)].sort(),
    unknown_provider: false,
  };
}

export function updateAdminModelProviderSettingDraftValue(
  draft: AdminModelProviderSettingsDraft,
  name: string,
  value: string,
): AdminModelProviderSettingsDraft {
  if (!Object.hasOwn(draft.values, name)) {
    throw new AdminModelProviderSettingsDraftError(
      `Provider setting ${name} is not editable`,
    );
  }
  return {
    ...draft,
    values: { ...draft.values, [name]: value },
  };
}

export function resetAdminModelProviderSettingDraftValue(
  descriptor: AdminModelProviderAdapterDescriptor,
  draft: AdminModelProviderSettingsDraft,
  name: string,
): AdminModelProviderSettingsDraft {
  const field = descriptor.setting_fields.find((item) => item.name === name);
  if (!field || isPreservedField(field)) {
    throw new AdminModelProviderSettingsDraftError(
      `Provider setting ${name} is not editable`,
    );
  }
  return updateAdminModelProviderSettingDraftValue(
    draft,
    name,
    defaultDraftValue(field),
  );
}

const OMIT_PROVIDER_DEFAULT = Symbol("omit-provider-default");

function parseDraftValue(
  field: AdminModelProviderSettingField,
  rawValue: string,
): AdminModelSettingValue | typeof OMIT_PROVIDER_DEFAULT {
  const value = rawValue.trim();
  if (!value) {
    if (field.default_mode === "provider") return OMIT_PROVIDER_DEFAULT;
    return field.default_value;
  }
  if (field.input_type === "boolean") {
    if (value === "true") return true;
    if (value === "false") return false;
    throw new AdminModelProviderSettingsDraftError();
  }
  if (field.input_type === "integer" || field.input_type === "number") {
    const parsed = Number(value);
    if (
      !Number.isFinite(parsed) ||
      (field.input_type === "integer" && !Number.isInteger(parsed))
    ) {
      throw new AdminModelProviderSettingsDraftError();
    }
    return parsed;
  }
  if (
    field.input_type === "enum" ||
    field.input_type === "string" ||
    field.input_type === "url"
  ) {
    return value;
  }
  throw new AdminModelProviderSettingsDraftError();
}

function settingValuesEqual(
  left: AdminModelSettingValue,
  right: AdminModelSettingValue,
): boolean {
  if (
    (typeof left === "string" ||
      typeof left === "number" ||
      typeof left === "boolean" ||
      left === null) &&
    (typeof right === "string" ||
      typeof right === "number" ||
      typeof right === "boolean" ||
      right === null)
  ) {
    return Object.is(left, right);
  }
  return JSON.stringify(left) === JSON.stringify(right);
}

export function serializeAdminModelProviderSettingsDraft(
  descriptor: AdminModelProviderAdapterDescriptor | null | undefined,
  draft: AdminModelProviderSettingsDraft,
): Record<string, AdminModelSettingValue> {
  if (!descriptor || draft.unknown_provider) {
    throw new AdminModelProviderSettingsDraftError(
      `Unknown Provider ${draft.provider_adapter}`,
    );
  }
  if (draft.incompatible_keys.length > 0) {
    throw new AdminModelProviderSettingsDraftError(
      `Unsupported provider settings: ${draft.incompatible_keys.join(", ")}`,
    );
  }

  const settings: Record<string, AdminModelSettingValue> = {
    ...draft.preserved_settings,
  };
  const originalKeys = new Set(draft.original_keys);
  for (const field of descriptor.setting_fields) {
    if (isPreservedField(field)) continue;
    const parsed = parseDraftValue(field, draft.values[field.name] ?? "");
    if (parsed === OMIT_PROVIDER_DEFAULT) continue;
    if (
      field.default_mode === "platform" &&
      !originalKeys.has(field.name) &&
      settingValuesEqual(parsed, field.default_value)
    ) {
      continue;
    }
    settings[field.name] = parsed;
  }

  return adminModelSettingsSchemaForProvider(descriptor).parse(settings);
}
