import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, get } from "./api";
import { useGame } from "../store/game";

/** GET with loading/error state, abort on unmount and a manual reload(). */
export function useApi<T>(path: string | null, query?: Record<string, any>, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(!!path);
  const [error, setError] = useState<string | null>(null);
  const [locked, setLocked] = useState<string | null>(null);
  const abort = useRef<AbortController | null>(null);
  const queryKey = JSON.stringify(query ?? {});

  const load = useCallback(
    async (silent = false) => {
      if (!path) return;
      abort.current?.abort();
      const ac = new AbortController();
      abort.current = ac;
      if (!silent) setLoading(true);
      setError(null);
      try {
        const res = await get<T>(path, query, ac.signal);
        if (!ac.signal.aborted) {
          setData(res);
          setLocked(null);
        }
      } catch (e) {
        if ((e as Error).name === "AbortError") return;
        const err = e as ApiError;
        if (err.code === "feature_locked") setLocked(String(err.data?.feature ?? ""));
        else setError(err.message);
      } finally {
        if (!ac.signal.aborted) setLoading(false);
      }
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [path, queryKey, ...deps],
  );

  useEffect(() => {
    load();
    return () => abort.current?.abort();
  }, [load]);

  return { data, loading, error, locked, reload: load, setData };
}

/** Wraps a mutating call: busy flag + error toast + optional success toast. */
export function useAction() {
  const [busy, setBusy] = useState(false);
  const toast = useGame((s) => s.toast);
  const run = useCallback(
    async <T,>(fn: () => Promise<T>, opts: { success?: string; onError?: (e: ApiError) => boolean | void } = {}): Promise<T | null> => {
      if (busy) return null;
      setBusy(true);
      try {
        const res = await fn();
        if (opts.success) toast(opts.success, "success");
        return res;
      } catch (e) {
        const err = e as ApiError;
        if (!opts.onError?.(err)) toast(err.message, "error");
        return null;
      } finally {
        setBusy(false);
      }
    },
    [busy, toast],
  );
  return { busy, run };
}
