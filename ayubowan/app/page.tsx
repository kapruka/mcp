import ChatInterface from '@/components/ChatInterface';
import Navbar from '@/components/Navbar';
import CartDrawer from '@/components/CartDrawer';

export default function Home() {
  return (
    <main className="flex h-screen flex-col bg-gray-100 relative">
      <Navbar />
      <div className="flex-1 overflow-hidden px-4">
        <ChatInterface />
      </div>
      <CartDrawer />
    </main>
  );
}