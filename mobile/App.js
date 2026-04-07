import React from 'react';
import { StatusBar } from 'expo-status-bar';
import { View, Text, StyleSheet } from 'react-native';
import { NavigationContainer } from '@react-navigation/native';
import { createBottomTabNavigator } from '@react-navigation/bottom-tabs';
import { SafeAreaProvider } from 'react-native-safe-area-context';
import { Ionicons } from '@expo/vector-icons';
import { COLORS } from './theme';

import DashboardScreen from './screens/DashboardScreen';
import SignalsScreen from './screens/SignalsScreen';
import StrategiesScreen from './screens/StrategiesScreen';
import TradeScreen from './screens/TradeScreen';
import AILabScreen from './screens/AILabScreen';
import SettingsScreen from './screens/SettingsScreen';

const Tab = createBottomTabNavigator();

const TAB_CONFIG = {
  Dashboard: { icon: 'grid-outline', activeIcon: 'grid' },
  Signals: { icon: 'pulse-outline', activeIcon: 'pulse' },
  Strategies: { icon: 'layers-outline', activeIcon: 'layers' },
  Trade: { icon: 'swap-vertical-outline', activeIcon: 'swap-vertical' },
  'AI Lab': { icon: 'sparkles-outline', activeIcon: 'sparkles' },
  Settings: { icon: 'settings-outline', activeIcon: 'settings' },
};

const DarkTheme = {
  dark: true,
  colors: {
    primary: COLORS.blue,
    background: COLORS.bg,
    card: COLORS.bgCard,
    text: COLORS.textPrimary,
    border: COLORS.border,
    notification: COLORS.blue,
  },
};

export default function App() {
  return (
    <SafeAreaProvider>
      <NavigationContainer theme={DarkTheme}>
        <StatusBar style="light" />
        <Tab.Navigator
          screenOptions={({ route }) => ({
            tabBarIcon: ({ focused, color, size }) => {
              const cfg = TAB_CONFIG[route.name] || {};
              const iconName = focused ? cfg.activeIcon : cfg.icon;
              return <Ionicons name={iconName} size={22} color={color} />;
            },
            tabBarActiveTintColor: COLORS.blue,
            tabBarInactiveTintColor: COLORS.textMuted,
            tabBarStyle: styles.tabBar,
            tabBarLabelStyle: styles.tabLabel,
            headerStyle: styles.header,
            headerTitleStyle: styles.headerTitle,
            headerTintColor: COLORS.textPrimary,
            headerRight: () => (
              <View style={styles.headerRight}>
                <View style={[styles.liveDot, { backgroundColor: COLORS.green }]} />
                <Text style={styles.liveText}>Live</Text>
              </View>
            ),
          })}
        >
          <Tab.Screen
            name="Dashboard"
            component={DashboardScreen}
            options={{ headerTitle: 'ProxiAlpha' }}
          />
          <Tab.Screen name="Signals" component={SignalsScreen} />
          <Tab.Screen name="Strategies" component={StrategiesScreen} />
          <Tab.Screen name="Trade" component={TradeScreen} />
          <Tab.Screen name="AI Lab" component={AILabScreen} />
          <Tab.Screen name="Settings" component={SettingsScreen} />
        </Tab.Navigator>
      </NavigationContainer>
    </SafeAreaProvider>
  );
}

const styles = StyleSheet.create({
  tabBar: {
    backgroundColor: COLORS.bgSidebar,
    borderTopColor: COLORS.border,
    borderTopWidth: 1,
    height: 85,
    paddingBottom: 20,
    paddingTop: 8,
  },
  tabLabel: {
    fontSize: 10,
    fontWeight: '500',
  },
  header: {
    backgroundColor: COLORS.bgSidebar,
    shadowColor: 'transparent',
    borderBottomWidth: 1,
    borderBottomColor: COLORS.border,
    elevation: 0,
  },
  headerTitle: {
    fontSize: 17,
    fontWeight: '700',
    color: COLORS.textPrimary,
  },
  headerRight: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 4,
    marginRight: 16,
  },
  liveDot: { width: 6, height: 6, borderRadius: 3 },
  liveText: { color: COLORS.green, fontSize: 12, fontWeight: '600' },
});
