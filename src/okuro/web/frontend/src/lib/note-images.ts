import { notesApi } from "@/lib/api";

/** Pull image files out of a paste/drop DataTransfer. Empty when none. */
export function imageFilesFrom(dt: DataTransfer | null): File[] {
  if (!dt) return [];
  const out: File[] = [];
  for (const item of Array.from(dt.items ?? [])) {
    if (item.kind === "file" && item.type.startsWith("image/")) {
      const f = item.getAsFile();
      if (f) out.push(f);
    }
  }
  // Fallback for drops that expose files but not items.
  if (out.length === 0) {
    for (const f of Array.from(dt.files ?? [])) {
      if (f.type.startsWith("image/")) out.push(f);
    }
  }
  return out;
}

/** Upload one image and return the markdown to insert (![alt](url)). */
export async function uploadImageToMarkdown(
  file: File,
  noteId?: string | null,
): Promise<string> {
  const { image } = await notesApi.imageUpload(file, noteId);
  const alt = file.name ? file.name.replace(/\.[^.]+$/, "") : "image";
  return `![${alt}](${image.url})`;
}
