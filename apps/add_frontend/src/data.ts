import { QueryClient } from '@tanstack/react-query'

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 10_000,
      gcTime: 10 * 60_000,
      retry: (count, error) => count < 2 && !(error instanceof Error && error.name === 'AbortError'),
      refetchOnWindowFocus: false,
    },
    mutations: { retry: false },
  },
})
