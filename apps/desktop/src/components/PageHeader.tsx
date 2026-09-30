import type { PropsWithChildren } from 'react';

// Shared page chrome; screens supply their own title, context and actions.
export function PageHeader({ children, className = '' }: PropsWithChildren<{ className?: string }>) {
  return <header className={`page-header ${className}`}>{children}</header>;
}
