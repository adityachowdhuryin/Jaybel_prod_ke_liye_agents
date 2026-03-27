import { ChatPanel } from "@/components/chat-panel";

export default function Home() {
  return (
    <main className="relative min-h-screen overflow-hidden bg-background">
      <div
        className="pointer-events-none absolute inset-0 bg-[radial-gradient(ellipse_80%_50%_at_50%_-20%,hsl(var(--primary)/0.12),transparent)]"
        aria-hidden
      />
      <div className="relative mx-auto flex min-h-screen max-w-4xl flex-col gap-8 px-4 py-10 md:px-8 md:py-14">
        <header className="space-y-3 text-center md:text-left">
          <p className="text-xs font-semibold uppercase tracking-[0.2em] text-muted-foreground">
            Local · Hybrid agent mesh
          </p>
          <h1 className="bg-gradient-to-br from-foreground to-foreground/70 bg-clip-text text-3xl font-semibold tracking-tight text-transparent md:text-4xl">
            Cost intelligence chat
          </h1>
          <p className="max-w-2xl text-sm leading-relaxed text-muted-foreground md:text-base">
            Talk to the conversational orchestrator on your machine. It routes
            cost and usage questions to the specialist agent over A2A; other
            topics get a clear, safe response.
          </p>
        </header>

        <ChatPanel />

        <footer className="pb-4 text-center text-xs text-muted-foreground md:text-left">
          Ensure the orchestrator and cost agent are running (e.g.{" "}
          <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-[0.8rem]">
            scripts/start-all.ps1
          </code>
          ).
        </footer>
      </div>
    </main>
  );
}
