type IconName =
  | "research"
  | "literature"
  | "routes"
  | "facts"
  | "tools"
  | "review"
  | "dossier"
  | "admin";

const paths: Record<IconName, string> = {
  research: "M4 5.5h6l2 2H20v11H4zM8 12h8M8 15h5",
  literature: "M5 4.5h6a3 3 0 0 1 3 3v12H8a3 3 0 0 0-3 1zM19 4.5h-2a3 3 0 0 0-3 3",
  routes: "M6 5v5a3 3 0 0 0 3 3h6a3 3 0 0 1 3 3v3M15 5h3v3M6 19h3",
  facts: "M12 3.5 19 7v10l-7 3.5L5 17V7zM8.5 12l2.2 2.2 4.8-5",
  tools: "M14.5 5.5a4 4 0 0 0-5 5L4 16l4 4 5.5-5.5a4 4 0 0 0 5-5l-3 3-3-3z",
  review: "M5 4.5h14v15H5zM8 9h8M8 13h5M16 16l1 1 2-2",
  dossier: "M6 3.5h9l3 3v14H6zM14 3.5v4h4M9 12h6M9 16h4",
  admin: "M12 3.5 19 6v5c0 4.6-2.8 7.6-7 9.5C7.8 18.6 5 15.6 5 11V6zM9.5 12l1.7 1.7 3.5-4",
};

export function Icon({ name }: { name: IconName }) {
  return (
    <svg aria-hidden="true" className="nav-icon" viewBox="0 0 24 24">
      <path d={paths[name]} />
    </svg>
  );
}
