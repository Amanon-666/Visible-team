declare module "*.module.css" {
  const classes: Record<string, string>;
  export const dispose: () => void;
  export default classes;
}
