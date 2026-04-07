/**
 * Shared design tokens for the ProxiAlpha mobile app.
 * Matches the dark-theme palette used in the web dashboard.
 */
export const COLORS = {
  // Backgrounds
  bg: '#0a0e17',
  bgCard: '#111827',
  bgCardHover: '#1a2332',
  bgInput: '#0d1321',
  bgSidebar: '#0d1117',
  bgModal: '#161f2e',

  // Borders
  border: '#1e293b',
  borderActive: '#3b82f6',

  // Text
  textPrimary: '#f1f5f9',
  textSecondary: '#94a3b8',
  textMuted: '#64748b',

  // Accent colors
  blue: '#3b82f6',
  blueDark: '#1e40af',
  green: '#10b981',
  greenDark: '#065f46',
  red: '#ef4444',
  redDark: '#991b1b',
  amber: '#f59e0b',
  amberDark: '#92400e',
  purple: '#8b5cf6',
  cyan: '#06b6d4',
  pink: '#ec4899',

  // Signal colors
  strongBuy: '#10b981',
  buy: '#34d399',
  hold: '#f59e0b',
  sell: '#f87171',
  strongSell: '#ef4444',
};

export const FONTS = {
  regular: { fontSize: 14, color: COLORS.textPrimary },
  small: { fontSize: 12, color: COLORS.textSecondary },
  label: { fontSize: 11, color: COLORS.textMuted, textTransform: 'uppercase', letterSpacing: 0.8 },
  heading: { fontSize: 20, fontWeight: '700', color: COLORS.textPrimary },
  subheading: { fontSize: 16, fontWeight: '600', color: COLORS.textPrimary },
  mono: { fontSize: 13, fontFamily: 'monospace', color: COLORS.textPrimary },
};

export const SHADOWS = {
  card: {
    shadowColor: '#000',
    shadowOffset: { width: 0, height: 2 },
    shadowOpacity: 0.25,
    shadowRadius: 4,
    elevation: 5,
  },
};

export const STRATEGY_COLORS = {
  DipBuyer: '#3b82f6',
  Technical: '#8b5cf6',
  DCA: '#06b6d4',
  CustomRules: '#f59e0b',
  AI_Claude: '#ec4899',
  Momentum: '#10b981',
  MeanReversion: '#f97316',
  Breakout: '#ef4444',
  TrendFollowing: '#14b8a6',
  PairsTrading: '#a855f7',
  EarningsPlay: '#eab308',
  SectorRotation: '#06b6d4',
  Scalping: '#f43f5e',
  SwingTrading: '#6366f1',
  OptionsFlow: '#d946ef',
};

export const SIGNAL_COLOR = {
  STRONG_BUY: COLORS.strongBuy,
  BUY: COLORS.buy,
  HOLD: COLORS.hold,
  SELL: COLORS.sell,
  STRONG_SELL: COLORS.strongSell,
};
