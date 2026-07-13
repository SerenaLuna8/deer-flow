import { AdminAssetsShell } from "@/components/admin/assets/admin-assets-shell";

export default function AssetsLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return <AdminAssetsShell>{children}</AdminAssetsShell>;
}
