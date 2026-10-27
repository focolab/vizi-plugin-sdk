package vizi.plugin.javasdk.manifest;

/**
 * Where a command or tool is listed (§3.2, §3.6). {@code "tools"} is the default; JSON {@code
 * null} registers the entry without listing it.
 */
public enum Menu {
  /** Listed in the Tools menu (the default). */
  TOOLS,
  /** Registered but not listed ({@code "menu": null}). */
  NONE
}
