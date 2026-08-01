type ScopeMarkProps = {
  className?: string;
};

export function ScopeMark({ className }: ScopeMarkProps) {
  return (
    <svg
      aria-hidden="true"
      className={className}
      fill="none"
      viewBox="0 0 32 32"
    >
      <rect height="25" rx="7" stroke="currentColor" width="25" x="3.5" y="3.5" />
      <circle cx="16" cy="16" r="7" stroke="currentColor" />
      <path d="M16 7v18M7 16h18" stroke="currentColor" />
      <circle cx="16" cy="16" fill="currentColor" r="2.25" />
    </svg>
  );
}
