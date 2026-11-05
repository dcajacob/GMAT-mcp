#pragma once
#include <QObject>
#include <QTcpServer>
#include <QJsonObject>
#include <QPointer>
class MainWindow;
class QTcpSocket;

// Explicitly enabled, single-controller, loopback bridge. All engine access is
// on MainWindow's thread; run-time event pumping permits only status/control.
class McpBridge final : public QObject
{
public:
   McpBridge(MainWindow &window,const QString &directory);
   ~McpBridge() override;
   void missionStarted();
   void missionFinished(int nativeStatus);
   QString projectDirectory() const;
private:
   QJsonObject dispatch(const QJsonObject &request,QTcpSocket *socket);
   QJsonObject state();
   void guard(const QJsonObject &args,bool version=false);
   MainWindow &window;
   QTcpServer server;
   QPointer<QTcpSocket> controller;
   QString token,desktopId,documentId,recordPath,root;
   const void *document=nullptr;
   QJsonObject run;
   QJsonObject pending;
   QJsonObject engineInfo;
   QString previousOutput;
   QString projectRoot;
};
