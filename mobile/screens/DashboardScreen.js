import React, { useState, useEffect, useCallback } from 'react';
import {
  View, Text, ScrollView, StyleSheet, TouchableOpacity,
  RefreshControl, ActivityIndicator, Dimensions,
} from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import { COLORS, STRATEGY_COLORS } from '../theme';
import Card from '../components/Card';
import MetricBox from '../components/MetricBox';
import Badge from '../components/Badge';
import api from '../api';

const { width: SCREEN_W } = Dimensions.get('window');

export default function DashboardScreen() {
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [connected, setConnected] = useState(false);
  const [health, setHealth] = useState(null);
  const [strategies, setStrategies] = useState([]);
  const [portfolio, setPortfolio] = useState(null);
  const [performance, setPerformance] = useState(null);

  const loadData = useCallback(async () => {
    try {
      const [h, s, p, perf] = await Promise.all([
        api.health().catch(() => null),
        api.getStrategies().catch(() => ({ strategies: [] })),
        api.getPortfolio().catch(() => null),
        api.getPerformance().catch(() => null),
      ]);
      setHealth(h);
      setConnected(!!h);
      setStrategies(s.strategies || []);
      setPortfolio(p);
      setPerformance(perf?.performance || null);
    } catch (e) {
      setConnected(false);
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => { loadData(); }, [loadData]);

  const onRefresh = () => { setRefreshing(true); loadData(); };

  const activeCount = strategies.filter(s => s.active).length;
  const totalStrats = strategies.length;

  const portfolioValue = portfolio?.state?.cash != null
    ? (portfolio.state.cash + Object.values(portfolio.state.positions || {}).reduce((sum, pos) => sum + (pos.shares * pos.avg_cost), 0))
    : 100000;

  const totalPnl = performance?.total_pnl || 0;
  const winRate = performance?.win_rate || 0;

  if (loading) {
    return (
      <View style={styles.center}>
        <ActivityIndicator size="large" color={COLORS.blue} />
        <Text style={styles.loadingText}>Connecting to server...</Text>
      </View>
    );
  }

  return (
    <ScrollView
      style={styles.container}
      contentContainerStyle={styles.content}
      refreshControl={<RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={COLORS.blue} />}
    >
      {/* Connection Status */}
      <View style={styles.statusRow}>
        <View style={[styles.statusDot, { backgroundColor: connected ? COLORS.green : COLORS.red }]} />
        <Text style={styles.statusText}>{connected ? 'Connected' : 'Offline'}</Text>
        {health && (
          <Text style={styles.statusMeta}>
            {health.strategies_loaded} strategies | {health.tickers_loaded} tickers
          </Text>
        )}
      </View>

      {/* Key Metrics Row */}
      <View style={styles.metricsRow}>
        <MetricBox
          label="Portfolio"
          value={`$${(portfolioValue / 1000).toFixed(1)}K`}
          color={COLORS.textPrimary}
        />
        <MetricBox
          label="P&L"
          value={`${totalPnl >= 0 ? '+' : ''}$${totalPnl.toFixed(0)}`}
          color={totalPnl >= 0 ? COLORS.green : COLORS.red}
        />
        <MetricBox
          label="Win Rate"
          value={`${(winRate * 100).toFixed(0)}%`}
          color={winRate >= 0.5 ? COLORS.green : COLORS.amber}
        />
      </View>

      {/* Active Strategies */}
      <Card>
        <View style={styles.cardHeader}>
          <Ionicons name="layers-outline" size={18} color={COLORS.blue} />
          <Text style={styles.cardTitle}>Active Strategies</Text>
          <Badge label={`${activeCount}/${totalStrats}`} color={COLORS.blue} small />
        </View>
        <View style={styles.stratGrid}>
          {strategies.filter(s => s.active).map(s => (
            <View key={s.name} style={[styles.stratChip, { borderColor: (STRATEGY_COLORS[s.name] || COLORS.blue) + '66' }]}>
              <View style={[styles.stratDot, { backgroundColor: STRATEGY_COLORS[s.name] || COLORS.blue }]} />
              <Text style={styles.stratName}>{s.name}</Text>
              <Text style={styles.stratWeight}>{s.weight.toFixed(1)}x</Text>
            </View>
          ))}
        </View>
      </Card>

      {/* Strategy Categories */}
      <Card>
        <View style={styles.cardHeader}>
          <Ionicons name="grid-outline" size={18} color={COLORS.purple} />
          <Text style={styles.cardTitle}>Strategy Categories</Text>
        </View>
        {Object.entries(groupByCategory(strategies)).map(([cat, strats]) => (
          <View key={cat} style={styles.categoryRow}>
            <Text style={styles.categoryName}>{cat}</Text>
            <View style={styles.categoryStrats}>
              {strats.map(s => (
                <View key={s.name} style={[styles.miniChip, { opacity: s.active ? 1 : 0.4 }]}>
                  <View style={[styles.tinyDot, { backgroundColor: STRATEGY_COLORS[s.name] || COLORS.blue }]} />
                  <Text style={styles.miniChipText}>{s.name}</Text>
                </View>
              ))}
            </View>
          </View>
        ))}
      </Card>

      {/* Portfolio Positions */}
      <Card>
        <View style={styles.cardHeader}>
          <Ionicons name="briefcase-outline" size={18} color={COLORS.green} />
          <Text style={styles.cardTitle}>Positions</Text>
        </View>
        {portfolio?.state?.positions && Object.keys(portfolio.state.positions).length > 0 ? (
          Object.entries(portfolio.state.positions).map(([ticker, pos]) => (
            <View key={ticker} style={styles.positionRow}>
              <View>
                <Text style={styles.posTicker}>{ticker}</Text>
                <Text style={styles.posShares}>{pos.shares} shares @ ${pos.avg_cost.toFixed(2)}</Text>
              </View>
              <Text style={[styles.posValue, { color: COLORS.textPrimary }]}>
                ${(pos.shares * pos.avg_cost).toFixed(0)}
              </Text>
            </View>
          ))
        ) : (
          <Text style={styles.emptyText}>No open positions</Text>
        )}
        {portfolio?.state?.cash != null && (
          <View style={styles.cashRow}>
            <Text style={styles.cashLabel}>Cash Available</Text>
            <Text style={styles.cashValue}>${portfolio.state.cash.toFixed(2)}</Text>
          </View>
        )}
      </Card>

      {/* Quick Actions */}
      <View style={styles.actionsRow}>
        <TouchableOpacity style={[styles.actionBtn, { backgroundColor: COLORS.blue + '22' }]} onPress={onRefresh}>
          <Ionicons name="refresh-outline" size={20} color={COLORS.blue} />
          <Text style={[styles.actionText, { color: COLORS.blue }]}>Refresh</Text>
        </TouchableOpacity>
        <TouchableOpacity style={[styles.actionBtn, { backgroundColor: COLORS.green + '22' }]}>
          <Ionicons name="scan-outline" size={20} color={COLORS.green} />
          <Text style={[styles.actionText, { color: COLORS.green }]}>Scan</Text>
        </TouchableOpacity>
        <TouchableOpacity style={[styles.actionBtn, { backgroundColor: COLORS.purple + '22' }]}>
          <Ionicons name="analytics-outline" size={20} color={COLORS.purple} />
          <Text style={[styles.actionText, { color: COLORS.purple }]}>Backtest</Text>
        </TouchableOpacity>
      </View>

      <View style={{ height: 40 }} />
    </ScrollView>
  );
}

function groupByCategory(strategies) {
  const groups = {};
  strategies.forEach(s => {
    const cat = s.category || 'Other';
    groups[cat] = groups[cat] || [];
    groups[cat].push(s);
  });
  return groups;
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: COLORS.bg },
  content: { padding: 16, paddingTop: 8 },
  center: { flex: 1, justifyContent: 'center', alignItems: 'center', backgroundColor: COLORS.bg },
  loadingText: { color: COLORS.textSecondary, marginTop: 12, fontSize: 14 },

  statusRow: { flexDirection: 'row', alignItems: 'center', marginBottom: 16, gap: 8 },
  statusDot: { width: 8, height: 8, borderRadius: 4 },
  statusText: { color: COLORS.textPrimary, fontSize: 14, fontWeight: '600' },
  statusMeta: { color: COLORS.textMuted, fontSize: 12, marginLeft: 'auto' },

  metricsRow: { flexDirection: 'row', marginBottom: 12, marginHorizontal: -4 },

  cardHeader: { flexDirection: 'row', alignItems: 'center', gap: 8, marginBottom: 12 },
  cardTitle: { fontSize: 15, fontWeight: '600', color: COLORS.textPrimary, flex: 1 },

  stratGrid: { flexDirection: 'row', flexWrap: 'wrap', gap: 8 },
  stratChip: {
    flexDirection: 'row', alignItems: 'center', gap: 6,
    backgroundColor: COLORS.bgInput, borderRadius: 8, borderWidth: 1,
    paddingHorizontal: 10, paddingVertical: 6,
  },
  stratDot: { width: 8, height: 8, borderRadius: 4 },
  stratName: { color: COLORS.textPrimary, fontSize: 12, fontWeight: '500' },
  stratWeight: { color: COLORS.textMuted, fontSize: 10 },

  categoryRow: { marginBottom: 10, flexDirection: 'row', gap: 10 },
  categoryName: { color: COLORS.textSecondary, fontSize: 12, fontWeight: '600', width: 80, paddingTop: 4 },
  categoryStrats: { flexDirection: 'row', flexWrap: 'wrap', flex: 1, gap: 6 },
  miniChip: { flexDirection: 'row', alignItems: 'center', gap: 4 },
  tinyDot: { width: 6, height: 6, borderRadius: 3 },
  miniChipText: { color: COLORS.textSecondary, fontSize: 11 },

  positionRow: {
    flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center',
    paddingVertical: 8, borderBottomWidth: 1, borderBottomColor: COLORS.border,
  },
  posTicker: { color: COLORS.textPrimary, fontSize: 14, fontWeight: '600' },
  posShares: { color: COLORS.textMuted, fontSize: 11, marginTop: 2 },
  posValue: { fontSize: 14, fontWeight: '600' },

  cashRow: {
    flexDirection: 'row', justifyContent: 'space-between', marginTop: 10,
    paddingTop: 8, borderTopWidth: 1, borderTopColor: COLORS.border,
  },
  cashLabel: { color: COLORS.textMuted, fontSize: 12 },
  cashValue: { color: COLORS.green, fontSize: 14, fontWeight: '600' },

  emptyText: { color: COLORS.textMuted, fontSize: 13, fontStyle: 'italic', textAlign: 'center', paddingVertical: 12 },

  actionsRow: { flexDirection: 'row', gap: 10, marginTop: 4 },
  actionBtn: {
    flex: 1, flexDirection: 'row', alignItems: 'center', justifyContent: 'center',
    gap: 6, paddingVertical: 14, borderRadius: 10,
  },
  actionText: { fontSize: 13, fontWeight: '600' },
});
