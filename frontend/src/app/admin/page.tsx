import { redirect } from "next/navigation";

export default function AdminRootPage(): never {
  redirect("/admin/operations");
}
