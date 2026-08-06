const STATIC_PROJECTS = [
  ["Research Lab", "Explore a local, read-only project workspace demo."],
  ["Product Studio", "Preview the project-first navigation and layout."],
  ["Operations", "See how independent project scopes stay separated."],
] as const;

export function WorkspacePage() {
  return (
    <main
      className="mx-auto min-h-screen w-full max-w-6xl space-y-8 px-6 py-12"
      data-testid="static-workspace-demo"
    >
      <header className="space-y-2">
        <h1 className="text-3xl font-semibold tracking-tight">Workspace</h1>
        <p className="text-muted-foreground">
          Local demo projects. Network-backed project actions are unavailable.
        </p>
      </header>
      <ul className="grid gap-4 md:grid-cols-3">
        {STATIC_PROJECTS.map(([name, description]) => (
          <li key={name} className="bg-card rounded-2xl border p-5 shadow-sm">
            <h2 className="font-medium">{name}</h2>
            <p className="text-muted-foreground mt-2 text-sm">{description}</p>
          </li>
        ))}
      </ul>
    </main>
  );
}

export function WorkspaceLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return children;
}
