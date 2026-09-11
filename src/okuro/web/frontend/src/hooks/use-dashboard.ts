import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";

interface BrainData {
  sessions: Array<{
    id: string;
    session_id: string;
    provider: string;
    task_hint: string;
    project: string;
    started_at: string;
    ended_at: string | null;
    compliance_normalized: number | null;
  }>;
  progress: Array<{
    id: string;
    project: string;
    agent: string;
    status: string;
    summary: string;
    updated_at: string;
  }>;
  thoughts: Array<{
    id: string;
    content: string;
    status: string;
    project: string;
    created_at: string;
  }>;
  memory: Array<{
    id: string;
    topic: string;
    content: string;
    project: string;
    confidence: number;
    source_agent: string;
    created_at: string;
  }>;
  tools: Array<{
    id: string;
    tool: string;
    server: string;
    latency_ms: number;
    ok: number;
    called_at: string;
  }>;
  projects: Array<{
    id: string;
    name: string;
    active: number;
    updated_at: string;
  }>;
}

interface TelemetryData {
  heatmap: Record<string, Record<string, number>>;
  providers: string[];
  hours: number[];
}

interface GpuData {
  gpus: Array<{
    id: number;
    name: string;
    model: string;
    vram: {
      total_mb: number;
      used_mb: number;
      free_mb: number;
      utilization_percent: number;
    };
    temperature_c: number;
    utilization_percent: number;
  }>;
  timestamp?: string;
}

interface HostData {
  cpu: {
    count_logical: number;
    count_physical: number | null;
    utilization_percent: number;
    per_core_percent: number[];
    frequency_mhz: number | null;
  };
  memory: {
    total_mb: number;
    used_mb: number;
    available_mb: number;
    utilization_percent: number;
  };
  swap: {
    total_mb: number;
    used_mb: number;
    utilization_percent: number;
  };
  timestamp: string;
}

export function useDashboardBrain() {
  return useQuery({
    queryKey: ["dashboard", "brain"],
    queryFn: () => api<BrainData>("/api/dashboard/brain"),
    refetchInterval: 15_000,
  });
}

type BrainPageData = Pick<BrainData, "sessions" | "progress" | "thoughts" | "memory">;

export function useBrain() {
  return useQuery({
    queryKey: ["brain", "full"],
    queryFn: () => api<BrainPageData>("/api/brain"),
    refetchInterval: 30_000,
  });
}

export function useDashboardTelemetry() {
  return useQuery({
    queryKey: ["dashboard", "telemetry"],
    queryFn: () => api<TelemetryData>("/api/dashboard/telemetry"),
    refetchInterval: 8_000,
  });
}

export function useDashboardGpu() {
  return useQuery({
    queryKey: ["dashboard", "gpu"],
    queryFn: () => api<GpuData>("/api/gpu"),
    refetchInterval: 8_000,
  });
}

export function useHost() {
  return useQuery({
    queryKey: ["host"],
    queryFn: () => api<HostData>("/api/host"),
    refetchInterval: 8_000,
  });
}
