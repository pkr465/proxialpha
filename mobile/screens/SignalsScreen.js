import React, { useState, useEffect, useCallback } from 'react';
import {
  View, Text, ScrollView, StyleSheet, TouchableOpacity,
  RefreshControl, ActivityIndicator, TextInput,
} from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import { COLORS, STRATEGY_COLORS } from '../theme';
import Card from '../components/Card';
import SignalBadge from '../components/SignalBadge';
import ConfidenceBar from '../components/ConfidenceBar';
import api from '../api';

export default function SignalsScreen() {
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [signals, setSignals] = useState([]);
  const [filter, setFilter] = useState('ALL');
  const [searchText, setSearchText] = useState('');
  const [scanning, setScanning] = useState(false);

  const loadSignals = useCallback(async () => {
    try {
      const data = await api.scan();
      setSignals(data.signals || []);
    } catch (e) {
      console.log('Signal load error:', e.message);
    } finally {
      setLoading(false);
      setRefreshing(false);
      setScanning(false);
    }
  }, []);

  useEffect(() => { loadSignals(); }, [loadSignals]);

  const onRefresh = () => { setRefreshing(true); loadSignals(); };
  const onScan = () => { setScanning(true); loadSignals(); };

  const FILTERS = ['ALL', 'STRONG_BUY', 'BUY', 'HOLD', 'SELL'];

  const filtered = signals.filter(s => {
    if (filter !== 'ALL' && s.signal !== filter) return false;
    if (searchText && !s.ticker?.toLowerCase().includes(searchText.toLowerCase())) return false;
    return true;
  });

  const counts = {
    ALL: signals.length,
    STRONG_BUY: signals.filter(s => s.signal === 'STRONG_BUY').length,
    BUY: signals.filter(s => s.signal === 'BUY').length,
    HOLD: signals.filter(s => s.signal === 'HOLD').length,
    SELL: signals.filter(s => s.signal === 'SELL' || s.signal === 'STRONG_SELL').length,
  };

  if (loading) {
    return (
      <View style={styles.center}>
        <ActivityIndicator size="large" color={COLORS.blue} />
        <Text style={styles.loadingText}>Scanning markets...</Text>
      </View>
    );
  }

  return (
    <ScrollView
      style={styles.container}
      contentContainerStyle={styles.content}
      refreshControl={<RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={COLORS.blue} />}
    >
      {/* Search + Scan */}
      <View style={styles.topRow}>
        <View style={styles.searchBox}>
          <Ionicons name="search-outline" size={16} color={COLORS.textMuted} />
          <TextInput
            style={styles.searchInput}
            placeholder="Search ticker..."
            placeholderTextColor={COLORS.textMuted}
            value={searchText}
            onChangeText={setSearchText}
          />
        </View>
        <TouchableOpacity style={styles.scanBtn} onPress={onScan} disabled={scanning}>
          {scanning ? (
            <ActivityIndicator size="small" color={COLORS.blue} />
          ) : (
            <>
              <Ionicons name="pulse-outline" size={16} color={COLORS.blue} />
              <Text style={styles.scanText}>Scan</Text>
            </>
          )}
        </TouchableOpacity>
      </View>

      {/* Filter Tabs */}
      <ScrollView horizontal showsHorizontalScrollIndicator={false} style={styles.filterRow}>
        {FILTERS.map(f => (
          <TouchableOpacity
            key={f}
            style={[styles.filterTab, filter === f && styles.filterActive]}
            onPress={() => setFilter(f)}
          >
            <Text style={[styles.filterText, filter === f && styles.filterTextActive]}>
              {f.replace('_', ' ')}
            </Text>
            <View style={[styles.filterCount, filter === f && styles.filterCountActive]}>
              <Text style={styles.filterCountText}>{counts[f] || 0}</Text>
            </View>
          </TouchableOpacity>
        ))}
      </ScrollView>

      {/* Signal Cards */}
      {filtered.length === 0 ? (
        <View style={styles.emptyState}>
          <Ionicons name="radio-outline" size={48} color={COLORS.textMuted} />
          <Text style={styles.emptyTitle}>No signals found</Text>
          <Text style={styles.emptySubtitle}>Try running a scan or adjusting filters</Text>
        </View>
      ) : (
        filtered.map((sig, idx) => (
          <SignalCard key={`${sig.ticker}-${idx}`} signal={sig} />
        ))
      )}

      <View style={{ height: 40 }} />
    </ScrollView>
  );
}

function SignalCard({ signal }) {
  const [expanded, setExpanded] = useState(false);

  return (
    <TouchableOpacity activeOpacity={0.8} onPress={() => setExpanded(!expanded)}>
      <Card style={styles.signalCard}>
        {/* Header Row */}
        <View style={styles.signalHeader}>
          <View style={{ flex: 1 }}>
            <View style={styles.tickerRow}>
              <Text style={styles.ticker}>{signal.ticker || '—'}</Text>
              <SignalBadge signal={signal.signal} />
            </View>
            <Text style={styles.strategy}>
              via {signal.strategy || 'Consensus'}
            </Text>
          </View>
          <View style={styles.priceCol}>
            <Text style={styles.price}>${(signal.price || 0).toFixed(2)}</Text>
            <ConfidenceBar value={signal.confidence || 0} />
          </View>
        </View>

        {/* Reasoning */}
        {signal.reasoning && (
          <Text style={styles.reasoning} numberOfLines={expanded ? 10 : 2}>
            {signal.reasoning}
          </Text>
        )}

        {/* Expanded Details */}
        {expanded && (
          <View style={styles.details}>
            {signal.target && (
              <DetailRow label="Target" value={`$${signal.target.toFixed(2)}`} color={COLORS.green} />
            )}
            {signal.stop_loss && (
              <DetailRow label="Stop Loss" value={`$${signal.stop_loss.toFixed(2)}`} color={COLORS.red} />
            )}
            {signal.position_size_pct && (
              <DetailRow label="Position Size" value={`${(signal.position_size_pct * 100).toFixed(1)}%`} color={COLORS.blue} />
            )}
            {signal.score != null && (
              <DetailRow label="Composite Score" value={signal.score.toFixed(2)} color={COLORS.purple} />
            )}
          </View>
        )}

        <View style={styles.expandHint}>
          <Ionicons
            name={expanded ? 'chevron-up-outline' : 'chevron-down-outline'}
            size={14} color={COLORS.textMuted}
          />
        </View>
      </Card>
    </TouchableOpacity>
  );
}

function DetailRow({ label, value, color }) {
  return (
    <View style={styles.detailRow}>
      <Text style={styles.detailLabel}>{label}</Text>
      <Text style={[styles.detailValue, { color }]}>{value}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: COLORS.bg },
  content: { padding: 16, paddingTop: 8 },
  center: { flex: 1, justifyContent: 'center', alignItems: 'center', backgroundColor: COLORS.bg },
  loadingText: { color: COLORS.textSecondary, marginTop: 12 },

  topRow: { flexDirection: 'row', gap: 10, marginBottom: 12 },
  searchBox: {
    flex: 1, flexDirection: 'row', alignItems: 'center', gap: 8,
    backgroundColor: COLORS.bgCard, borderRadius: 10, borderWidth: 1,
    borderColor: COLORS.border, paddingHorizontal: 12,
  },
  searchInput: { flex: 1, color: COLORS.textPrimary, fontSize: 14, paddingVertical: 10 },
  scanBtn: {
    flexDirection: 'row', alignItems: 'center', gap: 6,
    backgroundColor: COLORS.blue + '22', borderRadius: 10,
    paddingHorizontal: 16, paddingVertical: 10,
  },
  scanText: { color: COLORS.blue, fontSize: 13, fontWeight: '600' },

  filterRow: { marginBottom: 12 },
  filterTab: {
    flexDirection: 'row', alignItems: 'center', gap: 6,
    paddingHorizontal: 14, paddingVertical: 8, borderRadius: 20,
    backgroundColor: COLORS.bgCard, borderWidth: 1, borderColor: COLORS.border,
    marginRight: 8,
  },
  filterActive: { backgroundColor: COLORS.blue + '22', borderColor: COLORS.blue + '55' },
  filterText: { color: COLORS.textMuted, fontSize: 12, fontWeight: '500' },
  filterTextActive: { color: COLORS.blue },
  filterCount: { backgroundColor: COLORS.bgInput, borderRadius: 10, paddingHorizontal: 6, paddingVertical: 1 },
  filterCountActive: { backgroundColor: COLORS.blue + '33' },
  filterCountText: { color: COLORS.textMuted, fontSize: 10, fontWeight: '600' },

  emptyState: { alignItems: 'center', paddingVertical: 60 },
  emptyTitle: { color: COLORS.textSecondary, fontSize: 16, fontWeight: '600', marginTop: 12 },
  emptySubtitle: { color: COLORS.textMuted, fontSize: 13, marginTop: 4 },

  signalCard: { paddingBottom: 8 },
  signalHeader: { flexDirection: 'row', justifyContent: 'space-between' },
  tickerRow: { flexDirection: 'row', alignItems: 'center', gap: 8 },
  ticker: { color: COLORS.textPrimary, fontSize: 18, fontWeight: '700' },
  strategy: { color: COLORS.textMuted, fontSize: 11, marginTop: 2 },
  priceCol: { alignItems: 'flex-end', gap: 4 },
  price: { color: COLORS.textPrimary, fontSize: 16, fontWeight: '600', fontFamily: 'monospace' },

  reasoning: { color: COLORS.textSecondary, fontSize: 12, marginTop: 10, lineHeight: 18 },

  details: { marginTop: 10, gap: 6, borderTopWidth: 1, borderTopColor: COLORS.border, paddingTop: 10 },
  detailRow: { flexDirection: 'row', justifyContent: 'space-between' },
  detailLabel: { color: COLORS.textMuted, fontSize: 12 },
  detailValue: { fontSize: 13, fontWeight: '600' },

  expandHint: { alignItems: 'center', marginTop: 4 },
});
