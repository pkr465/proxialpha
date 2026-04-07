import React, { useState, useEffect, useCallback } from 'react';
import {
  View, Text, ScrollView, StyleSheet, TouchableOpacity,
  RefreshControl, ActivityIndicator, TextInput, Alert,
} from 'react-native';
import { Ionicons } from '@expo/vector-icons';
import { COLORS } from '../theme';
import Card from '../components/Card';
import api from '../api';

export default function TradeScreen() {
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [portfolio, setPortfolio] = useState(null);
  const [watchlist, setWatchlist] = useState([]);
  const [selectedTicker, setSelectedTicker] = useState('');
  const [action, setAction] = useState('BUY');
  const [amount, setAmount] = useState('');
  const [amountType, setAmountType] = useState('dollars'); // 'dollars' or 'shares'
  const [executing, setExecuting] = useState(false);
  const [trades, setTrades] = useState([]);

  const loadData = useCallback(async () => {
    try {
      const [p, w] = await Promise.all([
        api.getPortfolio().catch(() => null),
        api.getWatchlist().catch(() => ({ watchlist: [] })),
      ]);
      setPortfolio(p);
      setWatchlist(w.watchlist || []);
      setTrades(p?.state?.trades || []);
    } catch (e) {
      console.log('Trade load error:', e.message);
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => { loadData(); }, [loadData]);
  const onRefresh = () => { setRefreshing(true); loadData(); };

  const executeTrade = async () => {
    if (!selectedTicker) {
      Alert.alert('Select Ticker', 'Please select a stock to trade');
      return;
    }
    if (!amount || isNaN(Number(amount))) {
      Alert.alert('Enter Amount', 'Please enter a valid amount');
      return;
    }

    setExecuting(true);
    try {
      const shares = amountType === 'shares' ? parseInt(amount) : null;
      const dollars = amountType === 'dollars' ? parseFloat(amount) : null;
      const result = await api.executeTrade(selectedTicker, action, shares, dollars);
      Alert.alert(
        'Trade Executed',
        `${action} ${selectedTicker}\n${result.trade?.shares || 0} shares @ $${(result.trade?.price || 0).toFixed(2)}`
      );
      setAmount('');
      loadData();
    } catch (e) {
      Alert.alert('Trade Failed', e.message);
    } finally {
      setExecuting(false);
    }
  };

  const cash = portfolio?.state?.cash ?? 100000;
  const positions = portfolio?.state?.positions || {};

  if (loading) {
    return (
      <View style={styles.center}>
        <ActivityIndicator size="large" color={COLORS.blue} />
      </View>
    );
  }

  return (
    <ScrollView
      style={styles.container}
      contentContainerStyle={styles.content}
      refreshControl={<RefreshControl refreshing={refreshing} onRefresh={onRefresh} tintColor={COLORS.blue} />}
    >
      {/* Cash Balance */}
      <Card style={styles.balanceCard}>
        <Text style={styles.balanceLabel}>Available Cash</Text>
        <Text style={styles.balanceValue}>${cash.toLocaleString(undefined, { minimumFractionDigits: 2 })}</Text>
      </Card>

      {/* Trade Entry */}
      <Card>
        <View style={styles.cardHeader}>
          <Ionicons name="swap-vertical-outline" size={18} color={COLORS.blue} />
          <Text style={styles.cardTitle}>Execute Trade</Text>
        </View>

        {/* Action Toggle */}
        <View style={styles.actionRow}>
          <TouchableOpacity
            style={[styles.actionBtn, action === 'BUY' && styles.buyActive]}
            onPress={() => setAction('BUY')}
          >
            <Ionicons name="arrow-up-outline" size={16} color={action === 'BUY' ? '#fff' : COLORS.green} />
            <Text style={[styles.actionText, action === 'BUY' && { color: '#fff' }]}>BUY</Text>
          </TouchableOpacity>
          <TouchableOpacity
            style={[styles.actionBtn, action === 'SELL' && styles.sellActive]}
            onPress={() => setAction('SELL')}
          >
            <Ionicons name="arrow-down-outline" size={16} color={action === 'SELL' ? '#fff' : COLORS.red} />
            <Text style={[styles.actionText, action === 'SELL' && { color: '#fff' }]}>SELL</Text>
          </TouchableOpacity>
        </View>

        {/* Ticker Selector */}
        <Text style={styles.fieldLabel}>Ticker</Text>
        <ScrollView horizontal showsHorizontalScrollIndicator={false} style={styles.tickerScroll}>
          {watchlist.slice(0, 15).map(item => (
            <TouchableOpacity
              key={item.ticker}
              style={[styles.tickerChip, selectedTicker === item.ticker && styles.tickerChipActive]}
              onPress={() => setSelectedTicker(item.ticker)}
            >
              <Text style={[styles.tickerChipText, selectedTicker === item.ticker && styles.tickerChipTextActive]}>
                {item.ticker}
              </Text>
              <Text style={styles.tickerPrice}>${(item.current_price || 0).toFixed(0)}</Text>
            </TouchableOpacity>
          ))}
        </ScrollView>

        {/* Amount Type Toggle */}
        <Text style={styles.fieldLabel}>Amount</Text>
        <View style={styles.amountTypeRow}>
          <TouchableOpacity
            style={[styles.typeBtn, amountType === 'dollars' && styles.typeBtnActive]}
            onPress={() => setAmountType('dollars')}
          >
            <Text style={[styles.typeText, amountType === 'dollars' && styles.typeTextActive]}>$ Dollars</Text>
          </TouchableOpacity>
          <TouchableOpacity
            style={[styles.typeBtn, amountType === 'shares' && styles.typeBtnActive]}
            onPress={() => setAmountType('shares')}
          >
            <Text style={[styles.typeText, amountType === 'shares' && styles.typeTextActive]}>Shares</Text>
          </TouchableOpacity>
        </View>

        {/* Amount Input */}
        <TextInput
          style={styles.amountInput}
          placeholder={amountType === 'dollars' ? 'Enter amount ($)' : 'Enter shares'}
          placeholderTextColor={COLORS.textMuted}
          value={amount}
          onChangeText={setAmount}
          keyboardType="numeric"
        />

        {/* Execute Button */}
        <TouchableOpacity
          style={[styles.executeBtn, action === 'SELL' && styles.executeSell]}
          onPress={executeTrade}
          disabled={executing}
        >
          {executing ? (
            <ActivityIndicator size="small" color="#fff" />
          ) : (
            <>
              <Ionicons name={action === 'BUY' ? 'cart-outline' : 'exit-outline'} size={18} color="#fff" />
              <Text style={styles.executeText}>
                {action} {selectedTicker || '...'} {amount ? `(${amountType === 'dollars' ? '$' : ''}${amount}${amountType === 'shares' ? ' shares' : ''})` : ''}
              </Text>
            </>
          )}
        </TouchableOpacity>
      </Card>

      {/* Current Positions */}
      <Card>
        <View style={styles.cardHeader}>
          <Ionicons name="briefcase-outline" size={18} color={COLORS.green} />
          <Text style={styles.cardTitle}>Current Positions</Text>
        </View>
        {Object.keys(positions).length > 0 ? (
          Object.entries(positions).map(([ticker, pos]) => (
            <View key={ticker} style={styles.posRow}>
              <View>
                <Text style={styles.posTicker}>{ticker}</Text>
                <Text style={styles.posDetail}>{pos.shares} shares @ ${pos.avg_cost.toFixed(2)}</Text>
              </View>
              <View style={styles.posRight}>
                <Text style={styles.posValue}>${(pos.shares * pos.avg_cost).toFixed(0)}</Text>
                <TouchableOpacity
                  style={styles.quickSell}
                  onPress={() => { setSelectedTicker(ticker); setAction('SELL'); setAmount(String(pos.shares)); setAmountType('shares'); }}
                >
                  <Text style={styles.quickSellText}>Sell</Text>
                </TouchableOpacity>
              </View>
            </View>
          ))
        ) : (
          <Text style={styles.emptyText}>No open positions</Text>
        )}
      </Card>

      {/* Recent Trades */}
      <Card>
        <View style={styles.cardHeader}>
          <Ionicons name="time-outline" size={18} color={COLORS.amber} />
          <Text style={styles.cardTitle}>Recent Trades</Text>
        </View>
        {trades.length > 0 ? (
          trades.slice(-10).reverse().map((t, i) => (
            <View key={i} style={styles.tradeRow}>
              <View style={[styles.tradeSide, { backgroundColor: t.action === 'BUY' ? COLORS.green + '22' : COLORS.red + '22' }]}>
                <Text style={{ color: t.action === 'BUY' ? COLORS.green : COLORS.red, fontSize: 10, fontWeight: '700' }}>
                  {t.action}
                </Text>
              </View>
              <Text style={styles.tradeTicker}>{t.ticker}</Text>
              <Text style={styles.tradeDetail}>{t.shares} @ ${(t.price || 0).toFixed(2)}</Text>
              <Text style={styles.tradeDate}>{new Date(t.timestamp).toLocaleDateString()}</Text>
            </View>
          ))
        ) : (
          <Text style={styles.emptyText}>No trades yet</Text>
        )}
      </Card>

      <View style={{ height: 40 }} />
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: COLORS.bg },
  content: { padding: 16, paddingTop: 8 },
  center: { flex: 1, justifyContent: 'center', alignItems: 'center', backgroundColor: COLORS.bg },

  balanceCard: { alignItems: 'center', paddingVertical: 20 },
  balanceLabel: { fontSize: 11, color: COLORS.textMuted, textTransform: 'uppercase', letterSpacing: 1 },
  balanceValue: { fontSize: 32, fontWeight: '700', color: COLORS.green, marginTop: 4 },

  cardHeader: { flexDirection: 'row', alignItems: 'center', gap: 8, marginBottom: 12 },
  cardTitle: { fontSize: 15, fontWeight: '600', color: COLORS.textPrimary, flex: 1 },

  actionRow: { flexDirection: 'row', gap: 10, marginBottom: 16 },
  actionBtn: {
    flex: 1, flexDirection: 'row', alignItems: 'center', justifyContent: 'center',
    gap: 6, paddingVertical: 12, borderRadius: 10, borderWidth: 1, borderColor: COLORS.border,
    backgroundColor: COLORS.bgInput,
  },
  buyActive: { backgroundColor: COLORS.green, borderColor: COLORS.green },
  sellActive: { backgroundColor: COLORS.red, borderColor: COLORS.red },
  actionText: { fontSize: 14, fontWeight: '700', color: COLORS.textSecondary },

  fieldLabel: { fontSize: 11, color: COLORS.textMuted, textTransform: 'uppercase', letterSpacing: 0.6, marginBottom: 8 },

  tickerScroll: { marginBottom: 16 },
  tickerChip: {
    paddingHorizontal: 12, paddingVertical: 8, borderRadius: 8,
    backgroundColor: COLORS.bgInput, borderWidth: 1, borderColor: COLORS.border,
    marginRight: 8, alignItems: 'center',
  },
  tickerChipActive: { borderColor: COLORS.blue, backgroundColor: COLORS.blue + '22' },
  tickerChipText: { color: COLORS.textSecondary, fontSize: 12, fontWeight: '600' },
  tickerChipTextActive: { color: COLORS.blue },
  tickerPrice: { color: COLORS.textMuted, fontSize: 10, marginTop: 2 },

  amountTypeRow: { flexDirection: 'row', gap: 8, marginBottom: 12 },
  typeBtn: {
    flex: 1, paddingVertical: 8, borderRadius: 8,
    backgroundColor: COLORS.bgInput, borderWidth: 1, borderColor: COLORS.border,
    alignItems: 'center',
  },
  typeBtnActive: { borderColor: COLORS.blue, backgroundColor: COLORS.blue + '22' },
  typeText: { color: COLORS.textMuted, fontSize: 12, fontWeight: '500' },
  typeTextActive: { color: COLORS.blue },

  amountInput: {
    backgroundColor: COLORS.bgInput, borderRadius: 10, borderWidth: 1,
    borderColor: COLORS.border, paddingHorizontal: 14, paddingVertical: 12,
    color: COLORS.textPrimary, fontSize: 16, marginBottom: 16,
  },

  executeBtn: {
    flexDirection: 'row', alignItems: 'center', justifyContent: 'center', gap: 8,
    backgroundColor: COLORS.green, borderRadius: 10, paddingVertical: 14,
  },
  executeSell: { backgroundColor: COLORS.red },
  executeText: { color: '#fff', fontSize: 15, fontWeight: '700' },

  posRow: {
    flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center',
    paddingVertical: 10, borderBottomWidth: 1, borderBottomColor: COLORS.border,
  },
  posTicker: { color: COLORS.textPrimary, fontSize: 14, fontWeight: '600' },
  posDetail: { color: COLORS.textMuted, fontSize: 11, marginTop: 2 },
  posRight: { alignItems: 'flex-end', gap: 4 },
  posValue: { color: COLORS.textPrimary, fontSize: 14, fontWeight: '600' },
  quickSell: { paddingHorizontal: 8, paddingVertical: 3, backgroundColor: COLORS.red + '22', borderRadius: 6 },
  quickSellText: { color: COLORS.red, fontSize: 10, fontWeight: '600' },

  tradeRow: { flexDirection: 'row', alignItems: 'center', gap: 8, paddingVertical: 8, borderBottomWidth: 1, borderBottomColor: COLORS.border },
  tradeSide: { paddingHorizontal: 6, paddingVertical: 2, borderRadius: 4 },
  tradeTicker: { color: COLORS.textPrimary, fontSize: 13, fontWeight: '600', width: 50 },
  tradeDetail: { color: COLORS.textSecondary, fontSize: 12, flex: 1 },
  tradeDate: { color: COLORS.textMuted, fontSize: 10 },

  emptyText: { color: COLORS.textMuted, fontSize: 13, fontStyle: 'italic', textAlign: 'center', paddingVertical: 16 },
});
