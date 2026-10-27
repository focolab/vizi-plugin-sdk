package vizi.plugin.javasdk.doorbell;

import java.util.Arrays;

/**
 * A decoded {@code E} payload (doorbell_E_input.md §1). u32 fields are carried as {@code long}
 * (0 .. 2³²−1), u16 and u8 fields as {@code int}; nothing is sign-extended.
 *
 * <pre>
 *   [0)gen [4)t [8)interaction [12)seq [16)epoch [20)target_id [24)target_part [28)tool   u32
 *   [32)channels u16  [34)phase [35)view_id [36)level [37)mods [38)click_count [39)target_kind
 *   [40)payload_kind [41)position_flags  u8   [42)action u16  [44..92) position  [92..100) padding
 * </pre>
 *
 * @param position the 48 position bytes {@code [44..92)} as twelve f32 values, whatever the kind
 *     ({@code RAY_AND_PICKED}: camera, far, entry_frac, roll, picked z/y/x, back_frac)
 */
public record InputRecord(
    long gen,
    long t,
    long interaction,
    long seq,
    long epoch,
    long targetId,
    long targetPart,
    long tool,
    int channels,
    int phase,
    int viewId,
    int level,
    int mods,
    int clickCount,
    int targetKind,
    int payloadKind,
    int positionFlags,
    int action,
    float[] position) {

  public static final int PRESS = 0;
  public static final int DRAG = 1;
  public static final int RELEASE = 2;
  public static final int CANCEL = 3;
  public static final int FIRE = 4;

  public static final int KIND_NONE = 0;
  public static final int KIND_VOXEL = 1;
  public static final int KIND_PICKED = 2;
  public static final int KIND_COLUMN_YX = 3;
  public static final int KIND_COLUMN_ZX = 4;
  public static final int KIND_COLUMN_ZY = 5;
  public static final int KIND_RAY = 6;
  public static final int KIND_RAY_AND_PICKED = 7;

  public static final int TARGET_NONE = 0;
  public static final int TARGET_ANNOTATION = 1;
  public static final int TARGET_ROI = 2;
  public static final int TARGET_HANDLE = 3;

  public static final int MOD_SHIFT = 0x01;
  public static final int MOD_CTRL = 0x02;
  public static final int MOD_ALT = 0x04;
  public static final int MOD_META = 0x08;

  public static final int FLAG_MISS = 0x01;
  public static final int FLAG_ROLL_REF_Y = 0x02;

  /** bit15 of {@code channels}: a channel at index ≥ 15 is also visible (epoch state says which). */
  public static final int CHANNELS_HIGH = 0x8000;

  /** The f32 values of the position union, {@code [44..92)}. */
  public static final int POSITION_VALUES = 12;

  public InputRecord {
    if (position.length != POSITION_VALUES) throw new IllegalArgumentException("position is twelve f32 values");
    position = position.clone();
  }

  @Override
  public float[] position() {
    return position.clone();
  }

  public float position(int i) {
    return position[i];
  }

  /** PRESS and FIRE open an interaction; FIRE also closes it. */
  public boolean opening() {
    return phase == PRESS || phase == FIRE;
  }

  public boolean knownPhase() {
    return phase >= PRESS && phase <= FIRE;
  }

  public boolean knownPayloadKind() {
    return payloadKind >= KIND_NONE && payloadKind <= KIND_RAY_AND_PICKED;
  }

  public boolean knownTargetKind() {
    return targetKind >= TARGET_NONE && targetKind <= TARGET_HANDLE;
  }

  public boolean miss() {
    return (positionFlags & FLAG_MISS) != 0;
  }

  /** Bytes the position kind occupies; -1 for an unknown kind. */
  public static int positionBytes(int kind) {
    return switch (kind) {
      case KIND_NONE -> 0;
      case KIND_VOXEL, KIND_PICKED -> 12;
      case KIND_COLUMN_YX, KIND_COLUMN_ZX, KIND_COLUMN_ZY -> 16;
      case KIND_RAY -> 32;
      case KIND_RAY_AND_PICKED -> 48;
      default -> -1;
    };
  }

  static InputRecord decode(byte[] r) {
    int p = Records.PAYLOAD;
    float[] pos = new float[POSITION_VALUES];
    for (int i = 0; i < POSITION_VALUES; i++) pos[i] = Records.f32(r, p + 44 + 4 * i);
    return new InputRecord(
        Records.u32(r, p),
        Records.u32(r, p + 4),
        Records.u32(r, p + 8),
        Records.u32(r, p + 12),
        Records.u32(r, p + 16),
        Records.u32(r, p + 20),
        Records.u32(r, p + 24),
        Records.u32(r, p + 28),
        Records.u16(r, p + 32),
        Records.u8(r, p + 34),
        Records.u8(r, p + 35),
        Records.u8(r, p + 36),
        Records.u8(r, p + 37),
        Records.u8(r, p + 38),
        Records.u8(r, p + 39),
        Records.u8(r, p + 40),
        Records.u8(r, p + 41),
        Records.u16(r, p + 42),
        pos);
  }

  /**
   * Encodes this record. Position values past the kind's length are written as given, so a test
   * can build a record with a non-zero tail on purpose; {@link Builder} zeroes them.
   */
  public byte[] encode(long id) {
    byte[] pl = new byte[Records.PAYLOAD_SIZE];
    Records.putU32(pl, 0, gen);
    Records.putU32(pl, 4, t);
    Records.putU32(pl, 8, interaction);
    Records.putU32(pl, 12, seq);
    Records.putU32(pl, 16, epoch);
    Records.putU32(pl, 20, targetId);
    Records.putU32(pl, 24, targetPart);
    Records.putU32(pl, 28, tool);
    Records.putU16(pl, 32, channels);
    pl[34] = (byte) phase;
    pl[35] = (byte) viewId;
    pl[36] = (byte) level;
    pl[37] = (byte) mods;
    pl[38] = (byte) clickCount;
    pl[39] = (byte) targetKind;
    pl[40] = (byte) payloadKind;
    pl[41] = (byte) positionFlags;
    Records.putU16(pl, 42, action);
    for (int i = 0; i < POSITION_VALUES; i++) {
      if (Float.floatToRawIntBits(position[i]) != 0) Records.putF32(pl, 44 + 4 * i, position[i]);
    }
    return Records.seal(Records.OP_E, id, pl);
  }

  public static Builder builder() {
    return new Builder();
  }

  /** A convenience builder for hosts and tests; zero everywhere by default. */
  public static final class Builder {
    long gen = 1, t, interaction, seq, epoch = 1, targetId, targetPart, tool = 1;
    int channels = 1, phase, viewId, level, mods, clickCount, targetKind, payloadKind, positionFlags, action;
    float[] position = new float[POSITION_VALUES];

    public Builder gen(long v) { gen = v; return this; }
    public Builder t(long v) { t = v; return this; }
    public Builder interaction(long v) { interaction = v; return this; }
    public Builder seq(long v) { seq = v; return this; }
    public Builder epoch(long v) { epoch = v; return this; }
    public Builder target(int kind, long id, long part) { targetKind = kind; targetId = id; targetPart = part; return this; }
    public Builder tool(long v) { tool = v; return this; }
    public Builder channels(int v) { channels = v; return this; }
    public Builder phase(int v) { phase = v; return this; }
    public Builder viewId(int v) { viewId = v; return this; }
    public Builder level(int v) { level = v; return this; }
    public Builder mods(int v) { mods = v; return this; }
    public Builder clickCount(int v) { clickCount = v; return this; }
    public Builder flags(int v) { positionFlags = v; return this; }
    public Builder action(int v) { action = v; return this; }

    public Builder none() { payloadKind = KIND_NONE; position = new float[POSITION_VALUES]; return this; }

    public Builder voxel(float z, float y, float x) { return kind(KIND_VOXEL, z, y, x); }

    public Builder picked(float z, float y, float x) { return kind(KIND_PICKED, z, y, x); }

    public Builder columnYX(float y, float x, float zMin, float zMax) { return kind(KIND_COLUMN_YX, y, x, zMin, zMax); }

    public Builder ray(float[] camera, float[] far, float entryFrac, float roll) {
      return kind(KIND_RAY, camera[0], camera[1], camera[2], far[0], far[1], far[2], entryFrac, roll);
    }

    /**
     * A ray with the host's pick: the picked voxel and {@code backFrac}, how far the picked
     * structure runs behind its front as a fraction of the ray left there ({@code back = front +
     * backFrac × (far − front)}); {@code 0} a pick of no depth, {@code Float.NaN} nothing stood out.
     */
    public Builder rayAndPicked(float[] camera, float[] far, float entryFrac, float roll, float[] picked, float backFrac) {
      return kind(KIND_RAY_AND_PICKED, camera[0], camera[1], camera[2], far[0], far[1], far[2], entryFrac, roll,
          picked[0], picked[1], picked[2], backFrac);
    }

    /** Sets the kind and its leading values; the tail is zeroed. */
    public Builder kind(int kind, float... values) {
      payloadKind = kind;
      position = Arrays.copyOf(values, POSITION_VALUES);
      return this;
    }

    public InputRecord build() {
      return new InputRecord(gen, t, interaction, seq, epoch, targetId, targetPart, tool, channels, phase,
          viewId, level, mods, clickCount, targetKind, payloadKind, positionFlags, action, position);
    }
  }

  @Override
  public boolean equals(Object o) {
    return o instanceof InputRecord e && Arrays.equals(e.encode(0), encode(0));
  }

  @Override
  public int hashCode() {
    return Arrays.hashCode(encode(0));
  }

  @Override
  public String toString() {
    return "E{phase=" + phase + " interaction=" + interaction + " seq=" + seq + " tool=" + tool + " gen=" + gen
        + " view=" + viewId + " epoch=" + epoch + " kind=" + payloadKind + "}";
  }
}
