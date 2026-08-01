"use client";

interface RememberLoginFieldProps {
  checked: boolean;
  disabled?: boolean;
  label: string;
  onCheckedChange: (checked: boolean) => void;
}

export function RememberLoginField({
  checked,
  disabled = false,
  label,
  onCheckedChange,
}: RememberLoginFieldProps) {
  return (
    <label className="text-muted-foreground flex cursor-pointer items-center gap-2 text-sm">
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(event) => onCheckedChange(event.target.checked)}
        className="border-input accent-primary size-4 rounded"
      />
      <span>{label}</span>
    </label>
  );
}
