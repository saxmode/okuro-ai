import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { roleApi } from "@/lib/api";
import { toast } from "@/components/ui/toast";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogFooter,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

const TIERS = ["fast", "standard", "strategic"] as const;
const MODELS = ["haiku", "sonnet", "opus"] as const;

interface CreateRoleDialogProps {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  existingDomains: string[];
}

/**
 * Direct role creation (not via task decomposition). Wave 2 W2.3.
 */
export function CreateRoleDialog({
  open,
  onOpenChange,
  existingDomains,
}: CreateRoleDialogProps) {
  const qc = useQueryClient();
  const [roleId, setRoleId] = useState("");
  const [domain, setDomain] = useState("");
  const [description, setDescription] = useState("");
  const [tier, setTier] = useState<string>("standard");
  const [model, setModel] = useState<string>("sonnet");

  const reset = () => {
    setRoleId("");
    setDomain("");
    setDescription("");
    setTier("standard");
    setModel("sonnet");
  };

  const mutate = useMutation({
    mutationFn: roleApi.create,
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["roles"] });
      toast.success("Role created", { description: roleId });
      reset();
      onOpenChange(false);
    },
    onError: (e) =>
      toast.error("Create failed", {
        description: e instanceof Error ? e.message : "unknown error",
      }),
  });

  const canSubmit = roleId.trim() && domain.trim() && description.trim();

  return (
    <Dialog
      open={open}
      onOpenChange={(v) => {
        if (!v) reset();
        onOpenChange(v);
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle className="text-sm">Create role</DialogTitle>
        </DialogHeader>
        <div className="space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor="role-id" className="text-2xs uppercase tracking-wider text-tertiary">
              Role ID
            </Label>
            <Input
              id="role-id"
              value={roleId}
              onChange={(e) => setRoleId(e.target.value.replace(/\s+/g, "-").toLowerCase())}
              placeholder="e.g. security-reviewer"
              className="bg-surface"
            />
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="role-domain" className="text-2xs uppercase tracking-wider text-tertiary">
              Domain
            </Label>
            <Input
              id="role-domain"
              value={domain}
              onChange={(e) => setDomain(e.target.value.toLowerCase())}
              placeholder={existingDomains[0] ?? "engineering"}
              list="domain-suggestions"
              className="bg-surface"
            />
            <datalist id="domain-suggestions">
              {existingDomains.map((d) => (
                <option key={d} value={d} />
              ))}
            </datalist>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="role-desc" className="text-2xs uppercase tracking-wider text-tertiary">
              Description
            </Label>
            <Textarea
              id="role-desc"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="What does this role do? When should the orchestrator pick it?"
              rows={4}
              className="bg-surface text-sm"
            />
          </div>

          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-1.5">
              <Label className="text-2xs uppercase tracking-wider text-tertiary">Tier</Label>
              <Select value={tier} onValueChange={setTier}>
                <SelectTrigger className="bg-surface">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {TIERS.map((t) => (
                    <SelectItem key={t} value={t}>{t}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label className="text-2xs uppercase tracking-wider text-tertiary">Model</Label>
              <Select value={model} onValueChange={setModel}>
                <SelectTrigger className="bg-surface">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {MODELS.map((m) => (
                    <SelectItem key={m} value={m}>{m}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          </div>
        </div>
        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            disabled={!canSubmit || mutate.isPending}
            onClick={() =>
              mutate.mutate({
                role_id: roleId.trim(),
                domain: domain.trim(),
                description: description.trim(),
                tier,
                model,
              })
            }
          >
            {mutate.isPending ? "Creating…" : "Create role"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
