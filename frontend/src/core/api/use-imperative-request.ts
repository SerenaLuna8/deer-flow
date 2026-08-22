"use client";

import { useCallback, useEffect, useRef, useState } from "react";

export type ImperativeRequest<TInput, TResult> = {
  execute: (input: TInput) => Promise<TResult>;
  isPending: boolean;
  error: unknown;
  reset: () => void;
};

/**
 * Runs a request without placing its input or result in TanStack state.
 *
 * This is the browser boundary for write-only inputs such as API keys and
 * Configuration Secrets. The input exists only on the active promise stack;
 * React state retains pending/error metadata only.
 */
export function useImperativeRequest<TInput, TResult>(
  request: (input: TInput) => Promise<TResult>,
): ImperativeRequest<TInput, TResult> {
  const requestRef = useRef(request);
  const mountedRef = useRef(true);
  const activeCountRef = useRef(0);
  const [isPending, setIsPending] = useState(false);
  const [error, setError] = useState<unknown>(null);

  requestRef.current = request;

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const execute = useCallback(async (input: TInput) => {
    activeCountRef.current += 1;
    if (mountedRef.current) {
      setIsPending(true);
      setError(null);
    }
    try {
      return await requestRef.current(input);
    } catch (caught) {
      if (mountedRef.current) setError(caught);
      throw caught;
    } finally {
      activeCountRef.current -= 1;
      if (mountedRef.current && activeCountRef.current === 0) {
        setIsPending(false);
      }
    }
  }, []);

  const reset = useCallback(() => {
    if (mountedRef.current) setError(null);
  }, []);

  return { execute, isPending, error, reset };
}
