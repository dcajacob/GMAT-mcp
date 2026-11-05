#include "MainWindow.hpp"
#include "McpBridge.hpp"
#include "EditablePanel.hpp"
#include <QApplication>
#include <QPlainTextEdit>
#include <QTemporaryDir>
#include <QSettings>
#include <QDir>
#include <QFile>
#include <QJsonDocument>
#include <QJsonObject>
#include <QTcpSocket>
#include <QElapsedTimer>
#include <QThread>
#include <QMdiArea>
#include <QMdiSubWindow>
#include <QAction>
#include <iostream>
#include <stdexcept>
static void require(bool ok,const char *message) { if(!ok) throw std::runtime_error(message); }
class PendingPanel final:public EditablePanel {
public:
   bool hasChanges() const override { return true; }
   void discardChanges() override {}
};
int main(int argc,char **argv)
{
   QApplication app(argc,argv);
   app.setOrganizationName("GMATTests");app.setApplicationName("McpBridgeTests");
   try {
      require(argc==3,"Supply startup file and mission script");
      QTemporaryDir temp;require(temp.isValid(),"Temporary directory unavailable");
      QSettings::setDefaultFormat(QSettings::IniFormat);
      QSettings::setPath(QSettings::IniFormat,QSettings::UserScope,temp.path());
      QDir::setCurrent(QFileInfo(argv[1]).absolutePath());
      MainWindow window;window.show();
      require(window.initialize(QFileInfo(argv[1]).absoluteFilePath()),"Initialization failed");
      require(window.loadScript(QFileInfo(argv[2]).absoluteFilePath()) && window.buildScript(),"Mission build failed");
      McpBridge bridge(window,temp.filePath("desktops"));
      const auto files=QDir(temp.filePath("desktops")).entryList({"*.json"},QDir::Files);
      require(files.size()==1,"Discovery record absent");
      QFile file(temp.filePath("desktops/")+files.first());require(file.open(QIODevice::ReadOnly),"Discovery record unreadable");
      const auto record=QJsonDocument::fromJson(file.readAll()).object();
      QTcpSocket socket;socket.connectToHost("127.0.0.1",record["port"].toInt());require(socket.waitForConnected(3000),"Connection failed");
      const auto ask=[&](const QString &operation,const QJsonObject &arguments=QJsonObject(),bool authenticated=true) {
         socket.write(QJsonDocument(QJsonObject{{"token",authenticated?record["token"].toString():"invalid"},{"operation",operation},{"arguments",arguments}}).toJson(QJsonDocument::Compact)+'\n');socket.flush();
         QElapsedTimer timer;timer.start();
         while(!socket.canReadLine() && timer.elapsed()<5000) {QCoreApplication::processEvents();QThread::msleep(1);}
         require(socket.canReadLine(),"Bridge reply timed out");return QJsonDocument::fromJson(socket.readLine()).object();
      };
      require(!ask("hello",{},false)["ok"].toBool(),"Invalid token accepted");
      const auto hello=ask("hello");require(hello["ok"].toBool(),"Hello failed");
      require(!hello["result"].toObject().contains("token"),"Token exposed");
      auto state=ask("attach")["result"].toObject();
      const auto doc=state["document_id"].toString(),version=state["model_version"].toString();
      const QJsonObject args{{"document_id",doc},{"expected_version",version}};
      require(!ask("build",{{"document_id",doc},{"expected_version","stale"}})["ok"].toBool(),"Stale build accepted");
      auto *editor=window.findChild<QPlainTextEdit *>("scriptEditor");require(editor,"Editor absent");
      editor->appendPlainText("% GUI edit");
      state=ask("state")["result"].toObject();
      require(state["dirty"].toBool() && state["model_version"].toString()!=version,"GUI edit did not invalidate the version");
      require(!ask("apply_script",{{"document_id",doc},{"expected_version",state["model_version"]},{"text",editor->toPlainText()}})["ok"].toBool(),"Unbuilt editor overwritten");
      state=ask("build",{{"document_id",doc},{"expected_version",state["model_version"]}})["result"].toObject();
      require(!state["dirty"].toBool(),"Explicit build did not update model");
      auto *area=window.findChild<QMdiArea *>("workspace");require(area,"Workspace absent");
      auto *panelWindow=area->addSubWindow(new PendingPanel);
      auto pending=ask("build",{{"document_id",doc},{"expected_version",state["model_version"]}});
      require(!pending["ok"].toBool() && pending["error"].toObject()["code"]=="PENDING_PANEL_CHANGES","Pending panel edit ignored");
      delete panelWindow;
      const auto second=temp.filePath("second.script");require(QFile::copy(QFileInfo(argv[2]).absoluteFilePath(),second),"Second script unavailable");
      require(window.openScriptDocument(second),"Second document failed");
      auto *activate=window.findChild<QAction *>("activateScriptDocument");require(activate,"Activation action absent");activate->trigger();
      state=ask("state")["result"].toObject();
      require(state["document_id"].toString()!=doc,"Document switch was not observed");
      auto old=ask("get_object",{{"document_id",doc},{"name","Sat"}});
      require(!old["ok"].toBool() && old["error"].toObject()["code"]=="DOCUMENT_CHANGED","Old attachment followed a document switch");
      socket.disconnectFromHost();QCoreApplication::processEvents();
      require(window.isVisible(),"Disconnect closed the desktop");
      std::cout<<"PASS authentication, GUI edit versions, dirty editor, pending panels, explicit build, document binding, safe disconnect"<<std::endl;
      return 0;
   } catch(std::exception &error) {std::cerr<<"FAIL "<<error.what()<<std::endl;return 1;}
}
